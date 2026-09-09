"""Pure-Python unit tests for gateway_controller.py helpers.

No network, no filesystem side effects. Run with:

    python3 -m unittest discover -s tests -v

or via `make test` (which gates on unittest discover).

What's covered:
  - _validate_hostname: accept DNS-label strings, reject whitespace /
    newlines / semicolons / control characters
  - _redact_logs: strip IBKR account number patterns (DU\\d+, U\\d+)
    from arbitrary strings; pass non-matching strings through
  - _coerce_yes_no: accept yes/no/true/false/1/0/on/off, return None
    for empty or unrecognized values (so the caller knows to skip)
  - generate_totp: regression test against RFC 6238 SHA1 test vectors
    using a monkey-patched time.time()
  - api_port_for_mode: returns 4001 for live, 4002 for paper
  - Issue #23 self-restart adoption: _install4j_restarter_age
    (tempdir launcher layout), _AdoptedProcess (real throwaway child
    processes; /proc-only cases skipped off Linux),
    _adopt_self_restarted_gateway orchestration (collaborators
    mocked), _recover_jvm_or_escalate ordering, attempt_reauth /
    _redrive_login split

What's NOT covered by this file (tracked separately):
  - jts.ini writer (side effects on filesystem — needs tempdir fixture)
  - Agent protocol client (needs a mock socket server)
  - Live login flow (needs real Gateway + real credentials)
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch


def _load_module():
    """Load gateway_controller.py from the repo root.

    Returns the module object, reusable across tests. Called once at
    import time and cached at module level so each TestCase doesn't
    pay the startup cost.

    v0.5.14: the controller no longer imports gi.repository.Atspi at
    module load (the dead AT-SPI tree-walking helpers were removed
    along with the package install in the Dockerfile), so this loader
    no longer needs to stub the gi stack in sys.modules. A bare
    ``python3 gateway_controller.py`` works on any host with stdlib.
    """
    # The module does os.environ.get for several vars at load time;
    # most are optional but the controller checks USERNAME/PASSWORD
    # only inside main(), so we don't need to set them.
    os.environ.setdefault("TRADING_MODE", "paper")

    # Controller file is the sibling of tests/ in the repo layout.
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


class TestValidateHostname(unittest.TestCase):

    def test_accepts_simple_dns_label(self):
        self.assertEqual(
            gc._validate_hostname("cdc1.ibllc.com", "TWS_SERVER"),
            "cdc1.ibllc.com",
        )

    def test_accepts_another_real_example(self):
        self.assertEqual(
            gc._validate_hostname("ndc1.ibllc.com", "TWS_SERVER"),
            "ndc1.ibllc.com",
        )

    def test_accepts_hyphen_and_digit(self):
        self.assertEqual(
            gc._validate_hostname("host-1.example-co.com", "TWS_SERVER"),
            "host-1.example-co.com",
        )

    def test_accepts_empty_string(self):
        # Empty is allowed — it means "not set", fall back to Gateway's default
        self.assertEqual(
            gc._validate_hostname("", "TWS_SERVER"),
            "",
        )

    def test_rejects_newline(self):
        with self.assertRaisesRegex(ValueError, "not a valid hostname"):
            gc._validate_hostname("cdc1.ibllc.com\n[Logon]\nEvil=yes", "TWS_SERVER")

    def test_rejects_semicolon(self):
        with self.assertRaisesRegex(ValueError, "not a valid hostname"):
            gc._validate_hostname("cdc1.ibllc.com;evil", "TWS_SERVER")

    def test_rejects_space(self):
        with self.assertRaisesRegex(ValueError, "not a valid hostname"):
            gc._validate_hostname("cdc1.ibllc.com evil", "TWS_SERVER")

    def test_rejects_shell_metachar(self):
        with self.assertRaisesRegex(ValueError, "not a valid hostname"):
            gc._validate_hostname("cdc1.ibllc.com`id`", "TWS_SERVER")

    def test_rejects_pipe(self):
        with self.assertRaisesRegex(ValueError, "not a valid hostname"):
            gc._validate_hostname("cdc1.ibllc.com|nc attacker 4444", "TWS_SERVER")

    def test_error_message_names_the_variable(self):
        # Users need to know WHICH env var was bad
        try:
            gc._validate_hostname("bad space", "TWS_SERVER_PAPER")
        except ValueError as e:
            self.assertIn("TWS_SERVER_PAPER", str(e))
            self.assertIn("bad space", str(e))
        else:
            self.fail("should have raised ValueError")


class TestRedactLogs(unittest.TestCase):

    def test_redacts_paper_account_number(self):
        s = "DU9999999 Trader Workstation Configuration (Simulated Trading)"
        result = gc._redact_logs(s)
        self.assertIn("DU[REDACTED]", result)
        self.assertNotIn("DU9999999", result)
        self.assertIn("Trader Workstation Configuration", result)

    def test_redacts_live_account_number(self):
        self.assertEqual(
            gc._redact_logs("U1234567 Live Account"),
            "U[REDACTED] Live Account",
        )

    def test_passes_through_hostname(self):
        self.assertEqual(
            gc._redact_logs("cdc1.ibllc.com"),
            "cdc1.ibllc.com",
        )

    def test_passes_through_normal_log_line(self):
        self.assertEqual(
            gc._redact_logs("Login complete. Entering monitor loop."),
            "Login complete. Entering monitor loop.",
        )

    def test_passes_through_short_number(self):
        # Only DU/U followed by 5-10 digits should match. "DU123" is
        # too short and should pass through so we don't false-positive.
        self.assertEqual(gc._redact_logs("DU123"), "DU123")

    def test_handles_non_string(self):
        # The helper is defensive — non-strings pass through
        self.assertEqual(gc._redact_logs(None), None)
        self.assertEqual(gc._redact_logs(42), 42)
        self.assertEqual(gc._redact_logs([1, 2]), [1, 2])

    def test_redacts_multiple_in_one_string(self):
        s = "DU1111111 and DU2222222 and U3333333"
        result = gc._redact_logs(s)
        self.assertNotIn("DU1111111", result)
        self.assertNotIn("DU2222222", result)
        self.assertNotIn("U3333333", result)
        self.assertEqual(
            result,
            "DU[REDACTED] and DU[REDACTED] and U[REDACTED]",
        )


class TestCoerceYesNo(unittest.TestCase):

    def test_yes_values(self):
        for v in ["yes", "Yes", "YES", "true", "True", "TRUE",
                  "1", "on", "ON"]:
            self.assertEqual(gc._coerce_yes_no(v), True, f"failed on {v!r}")

    def test_no_values(self):
        for v in ["no", "No", "NO", "false", "False", "FALSE",
                  "0", "off", "OFF"]:
            self.assertEqual(gc._coerce_yes_no(v), False, f"failed on {v!r}")

    def test_empty_returns_none(self):
        self.assertIsNone(gc._coerce_yes_no(""))
        self.assertIsNone(gc._coerce_yes_no(None))

    def test_unrecognized_returns_none(self):
        self.assertIsNone(gc._coerce_yes_no("maybe"))
        self.assertIsNone(gc._coerce_yes_no("2"))
        self.assertIsNone(gc._coerce_yes_no("junk"))

    def test_whitespace_is_stripped(self):
        self.assertEqual(gc._coerce_yes_no("  yes  "), True)
        self.assertEqual(gc._coerce_yes_no("\tno\n"), False)


class TestGenerateTotp(unittest.TestCase):
    """Verify our TOTP against RFC 6238 appendix B SHA-1 test vectors.

    RFC 6238 uses the ASCII secret "12345678901234567890" (20 bytes)
    and several reference timestamps. Our implementation takes a
    base32 secret, so we convert the ASCII to base32 first.
    """

    SECRET = "12345678901234567890"
    # Base32-encoded version of the ASCII secret
    SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    def _at_time(self, unix_time):
        with patch.object(gc.time, "time", return_value=unix_time):
            return gc.generate_totp(self.SECRET_B32)

    def test_rfc6238_vector_59(self):
        # RFC 6238 appendix B: time=59, SHA-1 → 94287082 → last 6 digits "287082"
        self.assertEqual(self._at_time(59), "287082")

    def test_rfc6238_vector_1111111109(self):
        # RFC 6238: time=1111111109, SHA-1 → 07081804 → "081804"
        self.assertEqual(self._at_time(1111111109), "081804")

    def test_rfc6238_vector_1111111111(self):
        # RFC 6238: time=1111111111, SHA-1 → 14050471 → "050471"
        self.assertEqual(self._at_time(1111111111), "050471")

    def test_rfc6238_vector_1234567890(self):
        # RFC 6238: time=1234567890, SHA-1 → 89005924 → "005924"
        self.assertEqual(self._at_time(1234567890), "005924")

    def test_code_is_six_digits_zero_padded(self):
        # A synthetic case where the counter produces a value < 100000
        # should get zero-padded to 6 digits. We pick a time that
        # we happen to know produces such a value (the RFC vectors above
        # include "081804" which already starts with 0).
        self.assertEqual(len(self._at_time(1111111109)), 6)


class TestApiPortForMode(unittest.TestCase):
    """api_port_for_mode reads module-level TRADING_MODE. We set the
    module attribute directly for each test rather than re-importing."""

    def test_live_returns_4001(self):
        gc.TRADING_MODE = "live"
        self.assertEqual(gc.api_port_for_mode(), 4001)

    def test_paper_returns_4002(self):
        gc.TRADING_MODE = "paper"
        self.assertEqual(gc.api_port_for_mode(), 4002)


class TestDetectLoginStuckConnecting(unittest.TestCase):
    """_detect_login_stuck_connecting reads JLabel text via agent_labels
    and matches against the 'connecting to server' / 'trying for
    another' retry-loop signature. We mock agent_labels directly to
    exercise the positive + negative paths without a running agent."""

    def test_detects_connecting_to_server(self):
        with patch.object(gc, "agent_labels", return_value=[
            ("IB Gateway", "Attempt 3: connecting to server (trying for another 45 seconds)"),
        ]):
            self.assertTrue(gc._detect_login_stuck_connecting())

    def test_detects_trying_for_another(self):
        # Even if the "connecting to server" part gets truncated, the
        # "trying for another" substring alone is enough to flag the state.
        with patch.object(gc, "agent_labels", return_value=[
            ("IB Gateway", "trying for another 12 seconds"),
        ]):
            self.assertTrue(gc._detect_login_stuck_connecting())

    def test_case_insensitive(self):
        with patch.object(gc, "agent_labels", return_value=[
            ("IB Gateway", "Connecting To Server"),
        ]):
            self.assertTrue(gc._detect_login_stuck_connecting())

    def test_ignores_unrelated_labels(self):
        with patch.object(gc, "agent_labels", return_value=[
            ("IB Gateway", "Username"),
            ("IB Gateway", "Password"),
            ("IB Gateway", "Log In"),
        ]):
            self.assertFalse(gc._detect_login_stuck_connecting())

    def test_returns_false_on_empty_labels(self):
        with patch.object(gc, "agent_labels", return_value=[]):
            self.assertFalse(gc._detect_login_stuck_connecting())

    def test_returns_false_on_agent_exception(self):
        # If the agent socket is down we shouldn't raise; a false negative
        # here is safer than crashing the timeout handler.
        def boom():
            raise RuntimeError("agent socket closed")
        with patch.object(gc, "agent_labels", side_effect=boom):
            self.assertFalse(gc._detect_login_stuck_connecting())


class TestAttemptInplaceRelogin(unittest.TestCase):
    """attempt_inplace_relogin is the in-JVM relogin primitive. It must:
      - Never call launch_gateway / terminate / unlink-agent-socket
        (i.e. never touch process-lifecycle helpers).
      - Skip 'Connecting to server' progress dialogs (clicking OK on
        them cancels the login).
      - Dismiss recognized error modals via OK/Close.
      - Wait for the login frame (password text field) to reappear.
      - Re-drive handle_login on the same app reference and return its
        result.
    """

    def _fake_app(self):
        # The real app is an Atspi object; we only need something
        # identity-comparable for the assertion that handle_login was
        # called with the same reference the caller passed in.
        return object()

    def test_returns_false_when_login_frame_never_reappears(self):
        # v0.4.4: attempt_inplace_relogin probes with a short 2s timeout
        # first, then falls through to the full 120s wait if the probe
        # fails and the disposed-shell signature isn't matched. Both
        # calls return False here (frame genuinely gone), so the
        # function returns False without calling handle_login.
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[]), \
             patch.object(gc, "agent_wait_login_frame", return_value=False) as awlf, \
             patch.object(gc, "handle_login") as hl:
            self.assertFalse(gc.attempt_inplace_relogin(app))
            # Two calls: 2s probe, then 120s full wait (empty windows
            # list doesn't match the disposed-shell signature, so we
            # must not bail early).
            self.assertEqual(awlf.call_count, 2)
            hl.assert_not_called()

    def test_bails_on_disposed_shell_without_full_wait(self):
        # v0.4.4: after CCP lockout Gateway can dispose the login frame
        # entirely and transition into its post-auth "disconnected"
        # shell (single non-modal window titled "IBKR Gateway", no
        # JPasswordField anywhere). LoginManager.initiateLogin on the
        # captured reference is a silent no-op in that state, so in-JVM
        # relogin cannot recover. attempt_inplace_relogin must detect
        # the shell signature after a short probe and bail with False
        # so wait_for_api_port_with_retry escalates to container-level
        # kill+relaunch instead of burning 120s × 8 attempts.
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[
                ("ay", "IBKR Gateway", False),
             ]), \
             patch.object(gc, "agent_wait_login_frame", return_value=False) as awlf, \
             patch.object(gc, "handle_login") as hl:
            self.assertFalse(gc.attempt_inplace_relogin(app))
            # Only the 2s probe should run — NOT the full 120s wait.
            # That's the whole point: fast-fail so the outer loop
            # escalates instead of dead-waiting.
            self.assertEqual(awlf.call_count, 1)
            hl.assert_not_called()

    def test_calls_handle_login_on_same_app_when_frame_up(self):
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[]), \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=True) as hl:
            self.assertTrue(gc.attempt_inplace_relogin(app))
            # Critical: same app reference, no new JVM
            hl.assert_called_once_with(app)

    def test_propagates_handle_login_false(self):
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[]), \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=False):
            self.assertFalse(gc.attempt_inplace_relogin(app))

    def test_leaves_connecting_to_server_dialog_alone(self):
        # Clicking OK on the "Connecting to server" progress dialog
        # cancels the login. The helper MUST NOT click it.
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[
                ("frame", "Connecting to server", True),
             ]), \
             patch.object(gc, "agent_window", return_value="connecting to server (trying for another 30 seconds)"), \
             patch.object(gc, "agent_click_in_window") as click, \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=True):
            self.assertTrue(gc.attempt_inplace_relogin(app))
            click.assert_not_called()

    def test_dismisses_recognized_error_modal(self):
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[
                ("frame", "Login Error", True),
             ]), \
             patch.object(gc, "agent_window",
                          return_value="Login failed: server cannot be reached"), \
             patch.object(gc, "agent_click_in_window", return_value=True) as click, \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=True):
            self.assertTrue(gc.attempt_inplace_relogin(app))
            # Clicked OK (or Close) on the error modal
            self.assertTrue(click.called)
            first_call_title = click.call_args_list[0].args[0]
            self.assertEqual(first_call_title, "Login Error")

    def test_ignores_non_modal_windows(self):
        app = self._fake_app()
        with patch.object(gc, "agent_windows", return_value=[
                ("frame", "IBKR Gateway", False),  # not modal
             ]), \
             patch.object(gc, "agent_click_in_window") as click, \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=True):
            self.assertTrue(gc.attempt_inplace_relogin(app))
            click.assert_not_called()

    def test_swallows_agent_windows_exception(self):
        # Agent socket may flap during recovery; a transient failure
        # must not crash the retry loop. Fall through to the login-
        # frame wait regardless.
        app = self._fake_app()
        with patch.object(gc, "agent_windows", side_effect=RuntimeError("boom")), \
             patch.object(gc, "agent_wait_login_frame", return_value=True), \
             patch.object(gc, "handle_login", return_value=True) as hl:
            self.assertTrue(gc.attempt_inplace_relogin(app))
            hl.assert_called_once_with(app)


class TestWaitForApiPortWithRetry(unittest.TestCase):
    """wait_for_api_port_with_retry is v0.4.1's outer retry loop at the
    final auth indicator (the API port). It catches both CCP-Timeout
    and stuck-connecting lockout modes that the v0.4.0 main() outer
    loop misses. Behavior contract:
      - Port opens on first call -> return True, reset CCP backoff.
      - Port timeout + no lockout signature -> sys.exit(1) (terminal
        failure: wrong creds, wrong server, network).
      - Port timeout + CCP Timeout! OR stuck-connecting -> backoff,
        attempt_inplace_relogin, retry. Same app reference throughout.
      - Cap at _INPLACE_RELOGIN_MAX_ATTEMPTS relogins then sys.exit(1)
        for container-level recovery.
      - attempt_inplace_relogin failure -> sys.exit(1).
      - Eventual success resets CCP backoff.
    """

    def _fake_app(self):
        return object()

    def test_returns_true_immediately_on_success(self):
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", return_value=True), \
             patch.object(gc, "_reset_ccp_backoff") as reset, \
             patch.object(gc, "_detect_ccp_lockout") as ccp, \
             patch.object(gc, "_detect_login_stuck_connecting") as stuck, \
             patch.object(gc, "attempt_inplace_relogin") as relogin:
            self.assertTrue(gc.wait_for_api_port_with_retry(app))
            reset.assert_called_once()
            ccp.assert_not_called()
            stuck.assert_not_called()
            relogin.assert_not_called()

    def test_retries_on_ccp_lockout_signature(self):
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", side_effect=[False, True]), \
             patch.object(gc, "_detect_ccp_lockout", return_value=True), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=False), \
             patch.object(gc, "_apply_ccp_backoff") as backoff, \
             patch.object(gc, "_reset_ccp_backoff") as reset, \
             patch.object(gc, "attempt_inplace_relogin", return_value=True) as relogin:
            self.assertTrue(gc.wait_for_api_port_with_retry(app))
            backoff.assert_called_once()
            # Critical: same app reference, no new JVM
            relogin.assert_called_once_with(app)
            reset.assert_called_once()

    def test_retries_on_stuck_connecting_signature(self):
        # This is the bug-producing mode from v0.4.0 production: CCP
        # Timeout! never fires but the login dialog is stuck in its
        # "connecting to server" retry. Must still recover.
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", side_effect=[False, True]), \
             patch.object(gc, "_detect_ccp_lockout", return_value=False), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=True), \
             patch.object(gc, "_apply_ccp_backoff"), \
             patch.object(gc, "_reset_ccp_backoff") as reset, \
             patch.object(gc, "attempt_inplace_relogin", return_value=True) as relogin:
            self.assertTrue(gc.wait_for_api_port_with_retry(app))
            relogin.assert_called_once_with(app)
            reset.assert_called_once()

    def test_terminal_failure_when_no_lockout_signature(self):
        # Port didn't open AND neither detector fires. Treat as wrong-
        # creds / wrong-server / network failure. Must exit, must NOT
        # attempt relogin (no point retrying a terminal failure).
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", return_value=False), \
             patch.object(gc, "_detect_ccp_lockout", return_value=False), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=False), \
             patch.object(gc, "_diagnose_login_failure"), \
             patch.object(gc, "agent_windows", return_value=[]), \
             patch.object(gc, "agent_labels", return_value=[]), \
             patch.object(gc, "attempt_inplace_relogin") as relogin:
            with self.assertRaises(SystemExit) as ctx:
                gc.wait_for_api_port_with_retry(app)
            self.assertEqual(ctx.exception.code, 1)
            relogin.assert_not_called()

    def test_escalates_to_jvm_restart_on_max_attempts_exceeded(self):
        # v0.4.5: port never opens, CCP always detected, relogin
        # always succeeds. Loop caps at _INPLACE_RELOGIN_MAX_ATTEMPTS
        # and escalates to JVM restart via _escalate_to_jvm_restart
        # (no more sys.exit — dual-mode run.sh doesn't restart the
        # container on single-mode exit).
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", return_value=False), \
             patch.object(gc, "_detect_ccp_lockout", return_value=True), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=False), \
             patch.object(gc, "_apply_ccp_backoff"), \
             patch.object(gc, "_reset_ccp_backoff"), \
             patch.object(gc, "attempt_inplace_relogin", return_value=True) as relogin, \
             patch.object(gc, "_escalate_to_jvm_restart", return_value=True) as escalate:
            self.assertTrue(gc.wait_for_api_port_with_retry(app))
            self.assertEqual(relogin.call_count,
                             gc._INPLACE_RELOGIN_MAX_ATTEMPTS)
            escalate.assert_called_once()

    def test_escalates_to_jvm_restart_on_relogin_false(self):
        # v0.4.5: attempt_inplace_relogin returned False (disposed
        # login frame per v0.4.4, or handle_login failed). Must NOT
        # sys.exit — escalate to long-cool-down JVM restart.
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", return_value=False), \
             patch.object(gc, "_detect_ccp_lockout", return_value=True), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=False), \
             patch.object(gc, "_apply_ccp_backoff"), \
             patch.object(gc, "attempt_inplace_relogin", return_value=False) as relogin, \
             patch.object(gc, "_escalate_to_jvm_restart", return_value=True) as escalate:
            self.assertTrue(gc.wait_for_api_port_with_retry(app))
            relogin.assert_called_once()
            escalate.assert_called_once()

    def test_respects_custom_max_attempts(self):
        # Caller can override the cap (useful for tests / debugging).
        # v0.4.5: escalation fires after the custom cap.
        app = self._fake_app()
        with patch.object(gc, "wait_for_api_port", return_value=False), \
             patch.object(gc, "_detect_ccp_lockout", return_value=True), \
             patch.object(gc, "_detect_login_stuck_connecting", return_value=False), \
             patch.object(gc, "_apply_ccp_backoff"), \
             patch.object(gc, "attempt_inplace_relogin", return_value=True) as relogin, \
             patch.object(gc, "_escalate_to_jvm_restart", return_value=True) as escalate:
            self.assertTrue(gc.wait_for_api_port_with_retry(app, max_attempts=3))
            self.assertEqual(relogin.call_count, 3)
            escalate.assert_called_once()


class TestEscalateToJvmRestart(unittest.TestCase):
    """_escalate_to_jvm_restart is v0.4.5's dual-mode-aware recovery
    escape hatch. It replaces sys.exit(1) on CCP-exhaustion paths
    because run.sh's final ``wait "${pid[@]}"`` does not bring the
    container down when a single mode's controller exits — the
    container stays up on the other mode's PID.

    v0.4.6 contract: on each attempt, teardown the JVM first, THEN
    cool down, THEN relaunch. The teardown-before-cool-down ordering
    is the key v0.4.6 change — keeping the JVM alive during the
    cool-down lets its internal retry loop keep IBKR's CCP limiter
    armed, defeating the cool-down.
      - Each iteration: _teardown_jvm_for_restart, then
        _apply_ccp_long_cooldown, then _relaunch_and_login_in_place.
      - Returns True as soon as _relaunch_and_login_in_place is True.
      - Retries up to _JVM_RESTART_MAX_ATTEMPTS (default 5).
      - sys.exit(1) after cap exhaustion.
      - Resets CCP backoff on success.
    """

    # v0.5.9: halt-by-default is exercised by ``TestCcpPersistentHalt``
    # below. These tests opt back into the pre-v0.5.9 loop by patching
    # ``_CCP_LOCKOUT_MAX_JVM_RESTARTS`` to a positive value, which keeps
    # the invariants they pin (teardown-before-cooldown, retry-on-failure,
    # sys.exit after cap) testable without silent defaults.

    def test_returns_true_on_first_restart_success(self):
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=True) as relaunch, \
             patch.object(gc, "_reset_ccp_backoff") as reset:
            self.assertTrue(gc._escalate_to_jvm_restart("test reason"))
            teardown.assert_called_once()
            cooldown.assert_called_once()
            relaunch.assert_called_once()
            reset.assert_called_once()

    def test_teardown_fires_before_cooldown(self):
        # v0.4.6 core invariant: JVM must be killed before the long
        # silence, not after. Otherwise the JVM's internal
        # "Attempt N: connecting to server" retry loop keeps hitting
        # IBKR throughout the cool-down and the CCP limiter never clears.
        call_order = []
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart",
                          side_effect=lambda: call_order.append("teardown")), \
             patch.object(gc, "_apply_ccp_long_cooldown",
                          side_effect=lambda r, attempt=1: call_order.append(
                              f"cooldown(attempt={attempt})")), \
             patch.object(gc, "_relaunch_and_login_in_place",
                          side_effect=lambda: (call_order.append("relaunch") or True)), \
             patch.object(gc, "_reset_ccp_backoff"):
            gc._escalate_to_jvm_restart("test reason")
        # v0.5.5: cooldown is now invoked with attempt= kwarg so the
        # adaptive scaling sees the loop's 1-indexed retry counter.
        self.assertEqual(call_order, ["teardown", "cooldown(attempt=1)", "relaunch"])

    def test_retries_after_restart_failure(self):
        # Third relaunch succeeds — first two returned False. Teardown
        # and cool-down must fire before every relaunch attempt, not
        # just the first.
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place",
                          side_effect=[False, False, True]) as relaunch, \
             patch.object(gc, "_reset_ccp_backoff"):
            self.assertTrue(gc._escalate_to_jvm_restart("test reason"))
            self.assertEqual(teardown.call_count, 3)
            self.assertEqual(cooldown.call_count, 3)
            self.assertEqual(relaunch.call_count, 3)

    def test_exits_after_restart_cap(self):
        # Every relaunch fails. Must sys.exit(1) after the cap and not
        # loop forever.
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=False) as relaunch, \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertRaises(SystemExit) as ctx:
                gc._escalate_to_jvm_restart("test reason")
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(teardown.call_count, 5)
            self.assertEqual(cooldown.call_count, 5)
            self.assertEqual(relaunch.call_count, 5)


class TestCcpPersistentHalt(unittest.TestCase):
    """v0.5.9: CCP-lockout recovery is halt-by-default.

    Pre-v0.5.9, ``_escalate_to_jvm_restart`` ran 5 JVM-teardown cycles
    before giving up. On 2026-04-19 a production incident showed that
    each teardown's SIGKILL fallback re-stranded the IBKR session slot
    and extended IBKR's server-side zombie timer, so 5 retries
    compounded the lockout we were trying to clear. v0.5.9 makes the
    loop opt-in via ``CCP_LOCKOUT_MAX_JVM_RESTARTS``; default 0 emits
    ``ALERT_CCP_PERSISTENT_HALT`` and exits so an operator can clear
    the server-side state before the controller re-opens the auth
    pipe."""

    def _run_escalate_capturing_errors(self):
        errors = []
        with patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place") as relaunch, \
             patch.object(gc, "_reset_ccp_backoff"), \
             patch.object(gc.log, "error",
                          side_effect=lambda msg: errors.append(msg)), \
             patch.object(gc.log, "warning"):
            with self.assertRaises(SystemExit) as ctx:
                gc._escalate_to_jvm_restart("in-JVM relogin exhausted")
        return ctx, errors, teardown, cooldown, relaunch

    def test_default_env_halts_without_touching_jvm(self):
        """Default (env=0) must NOT call _teardown_jvm_for_restart —
        that's the whole point: each teardown's SIGKILL fallback is
        what re-strands the slot. Halt first, let operator intervene."""
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 0):
            ctx, errors, teardown, cooldown, relaunch = (
                self._run_escalate_capturing_errors())
        self.assertEqual(ctx.exception.code, 1)
        teardown.assert_not_called()
        cooldown.assert_not_called()
        relaunch.assert_not_called()

    def test_default_emits_ccp_persistent_halt_alert(self):
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 0):
            _ctx, errors, _t, _c, _r = self._run_escalate_capturing_errors()
        halt_hits = [m for m in errors
                     if m.startswith("ALERT_CCP_PERSISTENT_HALT ")]
        self.assertEqual(len(halt_hits), 1,
                         f"expected exactly one ALERT_CCP_PERSISTENT_HALT, "
                         f"got {len(halt_hits)}: {errors!r}")
        alert = halt_hits[0]
        self.assertIn(f"mode={gc.TRADING_MODE}", alert)
        self.assertIn('reason="in-JVM relogin exhausted"', alert)
        self.assertIn("remediation=", alert,
                      "operators need a remediation pointer in the grep line")

    def test_positive_env_preserves_old_loop_semantics(self):
        """Opt-in path: env=3 → exactly 3 teardown/cooldown/relaunch
        cycles before sys.exit. Confirms the pre-v0.5.9 behaviour is
        still reachable."""
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 3), \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place",
                          return_value=False) as relaunch, \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertRaises(SystemExit) as ctx:
                gc._escalate_to_jvm_restart("test")
        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(teardown.call_count, 3)
        self.assertEqual(cooldown.call_count, 3)
        self.assertEqual(relaunch.call_count, 3)

    def test_positive_env_halt_alert_not_emitted_when_loop_succeeds(self):
        """Opt-in path returns True on first success — no halt alert."""
        errors = []
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 3), \
             patch.object(gc, "_teardown_jvm_for_restart"), \
             patch.object(gc, "_apply_ccp_long_cooldown"), \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=True), \
             patch.object(gc, "_reset_ccp_backoff"), \
             patch.object(gc.log, "error",
                          side_effect=lambda msg: errors.append(msg)):
            gc._escalate_to_jvm_restart("test")
        halt_hits = [m for m in errors
                     if m.startswith("ALERT_CCP_PERSISTENT_HALT ")]
        self.assertEqual(halt_hits, [])


class TestRecoverJvmOrEscalate(unittest.TestCase):
    """_recover_jvm_or_escalate is v0.4.7's dual-mode-safe recovery
    helper for monitor_loop paths that previously sys.exit'd. Fast
    path via do_restart_in_place first (no cool-down); on failure
    fall through to _escalate_to_jvm_restart (silent cool-down).
    Contract: never returns False — returns True on recovery, or
    sys.exit(1) propagates from _escalate_to_jvm_restart's cap."""

    def test_returns_true_on_fast_restart_success(self):
        # Fast path succeeds — no escalation needed, no cool-down.
        with patch.object(gc, "do_restart_in_place", return_value=True) as restart, \
             patch.object(gc, "_escalate_to_jvm_restart") as escalate:
            self.assertTrue(gc._recover_jvm_or_escalate("test reason"))
            restart.assert_called_once()
            escalate.assert_not_called()

    def test_escalates_on_fast_restart_false(self):
        # do_restart_in_place returns False => escalate.
        with patch.object(gc, "do_restart_in_place", return_value=False) as restart, \
             patch.object(gc, "_escalate_to_jvm_restart",
                          return_value=True) as escalate:
            self.assertTrue(gc._recover_jvm_or_escalate("test reason"))
            restart.assert_called_once()
            escalate.assert_called_once_with("test reason")

    def test_escalates_on_fast_restart_exception(self):
        # Exception during do_restart_in_place must not propagate —
        # must be caught and routed to escalation.
        with patch.object(gc, "do_restart_in_place",
                          side_effect=RuntimeError("boom")) as restart, \
             patch.object(gc, "_escalate_to_jvm_restart",
                          return_value=True) as escalate:
            self.assertTrue(gc._recover_jvm_or_escalate("test reason"))
            restart.assert_called_once()
            escalate.assert_called_once_with("test reason")

    def test_propagates_systemexit_from_escalate_cap(self):
        # When escalate exhausts its cap and calls sys.exit(1), the
        # SystemExit must propagate up through _recover_jvm_or_escalate
        # (never swallowed).
        with patch.object(gc, "do_restart_in_place", return_value=False), \
             patch.object(gc, "_escalate_to_jvm_restart",
                          side_effect=SystemExit(1)):
            with self.assertRaises(SystemExit) as ctx:
                gc._recover_jvm_or_escalate("test reason")
            self.assertEqual(ctx.exception.code, 1)


class TestCcpLockoutStreak(unittest.TestCase):
    """v0.4.8: _detect_ccp_lockout tracks consecutive CCP lockouts.
    Streak >= 2 emits a concurrent-session warning naming that as the
    likely cause; streak >= 3 emits a structured ALERT_CCP_PERSISTENT
    ERROR token for external monitoring. _reset_ccp_backoff resets the
    streak on auth success.

    Cut future incident diagnosis time from hours (2026-04-17 incident:
    live stuck for 3h) to seconds."""

    def setUp(self):
        gc._ccp_lockout_streak = 0
        gc._ccp_backoff_seconds = 0.0

    def _run_detect_with_ccp_timeout(self):
        """Call _detect_ccp_lockout against a tempdir launcher.log
        containing the AuthTimeoutMonitor-CCP: Timeout! signature
        without a preceding NS_AUTH_START (= real CCP lockout)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, "launcher.log"), "w") as f:
                f.write("AuthTimeoutMonitor-CCP: activate\n")
                f.write("Authenticating\n")
                f.write("AuthTimeoutMonitor-CCP: Timeout!\n")
            with patch.object(gc, "JTS_CONFIG_DIR", tmpdir):
                return gc._detect_ccp_lockout(timeout=2)

    def test_streak_increments_on_each_lockout(self):
        self.assertEqual(gc._ccp_lockout_streak, 0)
        self.assertTrue(self._run_detect_with_ccp_timeout())
        self.assertEqual(gc._ccp_lockout_streak, 1)
        self.assertTrue(self._run_detect_with_ccp_timeout())
        self.assertEqual(gc._ccp_lockout_streak, 2)
        self.assertTrue(self._run_detect_with_ccp_timeout())
        self.assertEqual(gc._ccp_lockout_streak, 3)

    def test_first_lockout_no_concurrent_session_warning(self):
        with self.assertLogs("controller", level="WARNING") as ctx:
            self._run_detect_with_ccp_timeout()
        output = "\n".join(ctx.output)
        self.assertIn("CCP LOCKOUT DETECTED", output)
        self.assertNotIn("concurrent IBKR session", output)
        self.assertNotIn("ALERT_CCP_PERSISTENT", output)

    def test_second_lockout_emits_concurrent_session_warning(self):
        self._run_detect_with_ccp_timeout()  # streak=1
        with self.assertLogs("controller", level="WARNING") as ctx:
            self._run_detect_with_ccp_timeout()  # streak=2
        output = "\n".join(ctx.output)
        self.assertIn("concurrent IBKR session", output)
        self.assertIn("docs/DISCONNECT_RECOVERY.md", output)
        self.assertNotIn("ALERT_CCP_PERSISTENT", output)

    def test_third_lockout_emits_alert_token(self):
        self._run_detect_with_ccp_timeout()  # streak=1
        self._run_detect_with_ccp_timeout()  # streak=2
        with self.assertLogs("controller", level="ERROR") as ctx:
            self._run_detect_with_ccp_timeout()  # streak=3
        output = "\n".join(ctx.output)
        self.assertIn("ALERT_CCP_PERSISTENT", output)
        self.assertIn("consecutive_lockouts=3", output)
        self.assertIn("mode=", output)
        self.assertIn("suggested_action=", output)

    def test_fourth_lockout_still_emits_alert_token(self):
        for _ in range(3):
            self._run_detect_with_ccp_timeout()
        self.assertEqual(gc._ccp_lockout_streak, 3)
        with self.assertLogs("controller", level="ERROR") as ctx:
            self._run_detect_with_ccp_timeout()  # streak=4
        output = "\n".join(ctx.output)
        self.assertIn("ALERT_CCP_PERSISTENT", output)
        self.assertIn("consecutive_lockouts=4", output)

    def test_reset_ccp_backoff_resets_streak(self):
        self._run_detect_with_ccp_timeout()
        self._run_detect_with_ccp_timeout()
        self.assertEqual(gc._ccp_lockout_streak, 2)
        gc._reset_ccp_backoff()
        self.assertEqual(gc._ccp_lockout_streak, 0)

    def test_reset_streak_allows_fresh_diagnostic_cycle(self):
        # After reset, the next incident starts at streak=1 and must
        # NOT immediately emit the concurrent-session warning.
        for _ in range(3):
            self._run_detect_with_ccp_timeout()
        gc._reset_ccp_backoff()
        with self.assertLogs("controller", level="WARNING") as ctx:
            self._run_detect_with_ccp_timeout()  # fresh streak=1
        output = "\n".join(ctx.output)
        self.assertIn("CCP LOCKOUT DETECTED", output)
        self.assertNotIn("concurrent IBKR session", output)
        self.assertNotIn("ALERT_CCP_PERSISTENT", output)


class TestAlertJvmRestartExhausted(unittest.TestCase):
    """v0.4.9: after _JVM_RESTART_MAX_ATTEMPTS failed silent cool-down
    cycles, _escalate_to_jvm_restart emits the stable grep token
    ALERT_JVM_RESTART_EXHAUSTED before sys.exit(1). External monitoring
    greps this token to fire a Tier 1 push notification.

    Grep-contract for external monitors (see docs/OBSERVABILITY.md):
      ALERT_JVM_RESTART_EXHAUSTED mode=<live|paper> attempts=N reason="..."
    Stable prefix, key=value pairs, one line per terminal escalation."""

    def test_emits_alert_token_before_exit(self):
        # v0.5.9: opt into the pre-v0.5.9 JVM-restart loop so the
        # exhaustion branch is actually reachable. Default is halt.
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart"), \
             patch.object(gc, "_apply_ccp_long_cooldown"), \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=False), \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertLogs("controller", level="ERROR") as ctx:
                with self.assertRaises(SystemExit):
                    gc._escalate_to_jvm_restart("unit test exhaustion")
        output = "\n".join(ctx.output)
        self.assertIn("ALERT_JVM_RESTART_EXHAUSTED", output)
        self.assertIn("mode=", output)
        self.assertIn("attempts=5", output)
        self.assertIn("reason=\"unit test exhaustion", output)

    def test_no_alert_token_on_success_path(self):
        # Successful recovery must NOT emit the terminal alert token.
        # v0.5.9: same opt-in; default halt path never even tries.
        with patch.object(gc, "_CCP_LOCKOUT_MAX_JVM_RESTARTS", 5), \
             patch.object(gc, "_teardown_jvm_for_restart"), \
             patch.object(gc, "_apply_ccp_long_cooldown"), \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=True), \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertLogs("controller", level="INFO") as ctx:
                gc._escalate_to_jvm_restart("should succeed")
        output = "\n".join(ctx.output)
        self.assertNotIn("ALERT_JVM_RESTART_EXHAUSTED", output)


class TestLastAuthSuccessTs(unittest.TestCase):
    """v0.4.9: _reset_ccp_backoff records a wall-clock timestamp so the
    /health endpoint can report `last_auth_success_age_seconds`. Used
    by external monitoring to alert on 'logged in earlier but hasn't
    re-authed in too long'."""

    def setUp(self):
        gc._ccp_backoff_seconds = 0.0
        gc._ccp_lockout_streak = 0
        gc._last_auth_success_ts = None

    def test_starts_as_none(self):
        self.assertIsNone(gc._last_auth_success_ts)

    def test_reset_records_timestamp(self):
        before = time.time()
        gc._reset_ccp_backoff()
        after = time.time()
        self.assertIsNotNone(gc._last_auth_success_ts)
        self.assertGreaterEqual(gc._last_auth_success_ts, before)
        self.assertLessEqual(gc._last_auth_success_ts, after)

    def test_reset_updates_timestamp_each_call(self):
        gc._reset_ccp_backoff()
        first = gc._last_auth_success_ts
        time.sleep(0.01)
        gc._reset_ccp_backoff()
        self.assertGreater(gc._last_auth_success_ts, first)


class TestHealthSnapshot(unittest.TestCase):
    """v0.4.9: /health returns a JSON snapshot of the controller's
    current state. Healthy = state==MONITORING AND api_port_open AND
    JVM process still alive. Anything else = unhealthy (HTTP 503)."""

    def setUp(self):
        gc._current_state = gc.State.MONITORING
        gc.JVM_PID = 12345
        gc.GATEWAY_PROC = MagicMock()
        gc.GATEWAY_PROC.poll.return_value = None  # alive
        gc._ccp_lockout_streak = 0
        gc._ccp_backoff_seconds = 0.0
        gc._last_auth_success_ts = None

    def tearDown(self):
        gc.GATEWAY_PROC = None
        gc.JVM_PID = None

    def test_shape_contains_required_keys(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        for key in ("status", "version", "mode", "state", "jvm_pid",
                    "jvm_alive", "api_port", "api_port_open",
                    "last_auth_success_ts", "last_auth_success_age_seconds",
                    "ccp_lockout_streak", "ccp_backoff_seconds",
                    "uptime_seconds"):
            self.assertIn(key, snap, f"missing key: {key}")

    def test_healthy_when_monitoring_and_port_open(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["status"], "healthy")
        self.assertTrue(snap["api_port_open"])
        self.assertTrue(snap["jvm_alive"])

    def test_unhealthy_when_not_in_monitoring_state(self):
        gc._current_state = gc.State.LOGIN
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["status"], "unhealthy")
        self.assertEqual(snap["state"], "LOGIN")

    def test_unhealthy_when_api_port_closed(self):
        with patch.object(gc, "is_api_port_open", return_value=False):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["status"], "unhealthy")
        self.assertFalse(snap["api_port_open"])

    def test_unhealthy_when_jvm_dead(self):
        gc.GATEWAY_PROC.poll.return_value = 1  # exited with code 1
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["status"], "unhealthy")
        self.assertFalse(snap["jvm_alive"])

    def test_api_port_matches_trading_mode(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["api_port"], gc.api_port_for_mode())

    def test_last_auth_age_none_when_never_set(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertIsNone(snap["last_auth_success_ts"])
        self.assertIsNone(snap["last_auth_success_age_seconds"])

    def test_last_auth_age_computed_from_timestamp(self):
        gc._last_auth_success_ts = time.time() - 42.0
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertGreaterEqual(snap["last_auth_success_age_seconds"], 42.0)
        self.assertLess(snap["last_auth_success_age_seconds"], 45.0)

    def test_ccp_streak_and_backoff_surfaced(self):
        gc._ccp_lockout_streak = 3
        gc._ccp_backoff_seconds = 120.0
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["ccp_lockout_streak"], 3)
        self.assertEqual(snap["ccp_backoff_seconds"], 120.0)

    def test_serializes_cleanly_to_json(self):
        import json
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        # json.dumps raises if any value isn't serializable — critical
        # for the /health endpoint since it json.dumps the snapshot.
        body = json.dumps(snap)
        self.assertIsInstance(body, str)

    def test_version_field_is_module_version(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertEqual(snap["version"], gc.__version__)

    def test_uptime_is_nonnegative(self):
        with patch.object(gc, "is_api_port_open", return_value=True):
            snap = gc._build_health_snapshot()
        self.assertGreaterEqual(snap["uptime_seconds"], 0)


class TestDetectPasswordExpiry(unittest.TestCase):
    """v0.5.0: _detect_password_expiry() parses a dialog window-dump for
    Gateway/TWS password-expiry wording and returns ``(matched, status,
    days_remaining)``. ``status`` is ``"expired"`` (login blocked) or
    ``"warning"`` (advance notice). Downstream handler emits
    ``ALERT_PASSWORD_EXPIRED status=...`` based on the three-state return.

    Grep-contract for external monitors (see docs/OBSERVABILITY.md):
      ALERT_PASSWORD_EXPIRED status=<warning|expired> mode=<live|paper> [days_remaining=N] suggested_action="..."
    """

    def test_warning_variant_with_days(self):
        dump = "Password Notice\nYour password will expire in 14 days."
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "warning")
        self.assertEqual(days, 14)

    def test_warning_variant_days_singular(self):
        dump = "Your password will expire in 1 day. Please change it."
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "warning")
        self.assertEqual(days, 1)

    def test_expired_variant_no_days(self):
        dump = "Your password has expired. You must change it now."
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "expired")
        self.assertIsNone(days)

    def test_case_insensitive(self):
        dump = "YOUR PASSWORD WILL EXPIRE IN 7 DAYS"
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "warning")
        self.assertEqual(days, 7)

    def test_no_match_on_unrelated_dialog(self):
        matched, status, days = gc._detect_password_expiry(
            "Existing session detected. Click Continue Login to proceed.")
        self.assertFalse(matched)
        self.assertIsNone(status)
        self.assertIsNone(days)

    def test_no_match_on_empty_input(self):
        matched, status, days = gc._detect_password_expiry("")
        self.assertFalse(matched)
        self.assertIsNone(status)
        self.assertIsNone(days)

    def test_no_match_on_none_input(self):
        matched, status, days = gc._detect_password_expiry(None)
        self.assertFalse(matched)
        self.assertIsNone(status)
        self.assertIsNone(days)

    def test_matches_expires_in_variant(self):
        # Some TWS builds use "expires in N days" instead of "will expire"
        dump = "Password notice: expires in 30 days."
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "warning")
        self.assertEqual(days, 30)

    def test_warning_without_days_falls_back_to_warning_status(self):
        # "will expire" with no day count — operator still gets a warning,
        # but days_remaining is None (not zero, to avoid confusion with
        # the expired variant).
        dump = "Your password will expire soon. Please change it."
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "warning")
        self.assertIsNone(days)

    def test_expired_takes_precedence_over_warning(self):
        # Defensive: a dialog that includes both phrases should resolve
        # to 'expired' since that's the blocking state.
        dump = ("Your password has expired; it will expire in 0 days "
                "if not changed.")
        matched, status, days = gc._detect_password_expiry(dump)
        self.assertTrue(matched)
        self.assertEqual(status, "expired")
        self.assertIsNone(days)


class TestDetectBadCredentials(unittest.TestCase):
    """_detect_bad_credentials() recognizes Gateway's credential-rejection
    modal wording ("Invalid username or password" and close variants) so
    handle_post_login_dialogs / attempt_inplace_relogin can emit the
    ALERT_LOGIN_FAILED grep-contract token and dismiss the dialog.

    Grep-contract for external monitors (see docs/OBSERVABILITY.md):
      ALERT_LOGIN_FAILED mode=<live|paper> reason="bad-credentials" suggested_action="..."
    """

    def test_matches_canonical_invalid_username_or_password_modal(self):
        dump = ('Connection to server failed: Invalid username or '
                'password. Please check the Caps Lock key; passwords '
                'are case sensitive.')
        self.assertTrue(gc._detect_bad_credentials(dump))

    def test_matches_two_word_user_name_spelling(self):
        # Some IBKR builds render the two-word "user name".
        self.assertTrue(gc._detect_bad_credentials(
            "Invalid user name or password."))

    def test_matches_password_is_incorrect_variant(self):
        self.assertTrue(gc._detect_bad_credentials(
            "Username or password is incorrect."))

    def test_matches_credentials_rejected_variant(self):
        self.assertTrue(gc._detect_bad_credentials(
            "Connection failed because credentials were rejected."))

    def test_case_insensitive(self):
        self.assertTrue(gc._detect_bad_credentials(
            "INVALID USERNAME OR PASSWORD"))

    def test_no_match_on_progress_dialog(self):
        self.assertFalse(gc._detect_bad_credentials(
            "Connecting to server. Please wait."))

    def test_no_match_on_password_expiry_wording(self):
        # Must not collide with the password-expiry branch that runs first.
        self.assertFalse(gc._detect_bad_credentials(
            "Your password will expire in 14 days."))

    def test_no_match_on_empty_input(self):
        self.assertFalse(gc._detect_bad_credentials(""))

    def test_no_match_on_none_input(self):
        self.assertFalse(gc._detect_bad_credentials(None))


class TestResolveSafeDismissButtons(unittest.TestCase):
    """v0.5.1: _resolve_safe_dismiss_buttons() builds the ordered
    dismiss allowlist from BYPASS_WARNING. Returns a tuple so
    click-preference is deterministic and the same order is consumed
    by both dismiss_post_login_disclaimers() and wait_for_api_port()'s
    opportunistic sweep — closing the v0.5.0 gap where BYPASS_WARNING
    only took effect in one of the two paths.
    """

    def _call_with_env(self, value):
        env = dict(os.environ)
        if value is None:
            env.pop("BYPASS_WARNING", None)
        else:
            env["BYPASS_WARNING"] = value
        with patch.dict(os.environ, env, clear=True):
            return gc._resolve_safe_dismiss_buttons()

    def test_returns_tuple_not_set(self):
        result = self._call_with_env(None)
        self.assertIsInstance(result, tuple)

    def test_defaults_present_and_ordered(self):
        result = self._call_with_env(None)
        self.assertEqual(result, gc._DEFAULT_SAFE_DISMISS_BUTTONS)

    def test_bypass_warning_empty_returns_defaults(self):
        result = self._call_with_env("")
        self.assertEqual(result, gc._DEFAULT_SAFE_DISMISS_BUTTONS)

    def test_bypass_warning_single_value_appended_after_defaults(self):
        result = self._call_with_env("Continue")
        self.assertEqual(result[: len(gc._DEFAULT_SAFE_DISMISS_BUTTONS)],
                         gc._DEFAULT_SAFE_DISMISS_BUTTONS)
        self.assertEqual(result[-1], "Continue")

    def test_bypass_warning_comma_separated_preserves_order(self):
        result = self._call_with_env("Continue,Acknowledge Acknowledge,Foo")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue", "Acknowledge Acknowledge", "Foo"))

    def test_bypass_warning_semicolon_also_parsed(self):
        result = self._call_with_env("Continue;Foo;Bar")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue", "Foo", "Bar"))

    def test_bypass_warning_refuses_bare_ok(self):
        result = self._call_with_env("Continue,OK,Foo")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue", "Foo"))

    def test_bypass_warning_refuses_ok_case_insensitive(self):
        result = self._call_with_env("ok,Ok,OK,oK,Continue")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue",))

    def test_bypass_warning_dedupes_against_defaults(self):
        # "I Accept" is already in the defaults; repeating it should
        # not produce a duplicate entry.
        result = self._call_with_env("I Accept,Continue")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue",))
        self.assertEqual(
            result.count("I Accept"), 1,
            "defaults should not be duplicated when BYPASS_WARNING repeats them")

    def test_bypass_warning_dedupes_user_repeats(self):
        result = self._call_with_env("Continue,Continue,Continue")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue",))

    def test_bypass_warning_strips_whitespace(self):
        result = self._call_with_env("  Continue  ,  Foo  ")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue", "Foo"))

    def test_bypass_warning_ignores_empty_tokens(self):
        result = self._call_with_env("Continue,,,Foo,")
        extras = result[len(gc._DEFAULT_SAFE_DISMISS_BUTTONS):]
        self.assertEqual(extras, ("Continue", "Foo"))


class TestShutdownAlert(unittest.TestCase):
    """v0.5.2: shutdown() emits ALERT_SHUTDOWN with a documented format.

    The grep-contract in docs/OBSERVABILITY.md promises specific key
    names (mode=, signal=, graceful=, reason=) — if a refactor drops
    or renames any of them, external monitors break silently. These
    tests pin the format so that breakage fails CI instead of surfacing
    in prod."""

    def _run_shutdown(self, signum, proc_behavior="clean",
                      clean_logout_result=None, state=None):
        """Invoke shutdown() with side effects suppressed; return the
        list of log.info messages it emitted.

        proc_behavior:
          "absent" — GATEWAY_PROC is None (no JVM started yet)
          "exited" — JVM already exited (poll returns 0)
          "clean"  — terminate() + wait() succeed
          "stuck"  — wait() raises TimeoutExpired, kill() succeeds

        clean_logout_result: tuple (success, status, reason) controlling
        what ``_attempt_state_aware_clean_logout`` returns. Default
        forces the ``failed_unreachable`` path so tests exercise the
        SIGTERM fallback unless explicitly opting into the v0.5.6
        clean-logout behaviour.

        state: controller State to pin. Default ``State.MONITORING``
        preserves the v0.5.6 behaviour the original tests were written
        against; v0.5.9 state-aware tests pass an explicit earlier
        state to exercise the pre-MONITORING paths.
        """
        import subprocess
        info_calls = []

        class FakeProc:
            def __init__(self, behavior):
                self.behavior = behavior
                self.pid = 12345

            def poll(self):
                return 0 if self.behavior == "exited" else None

            def terminate(self):
                pass

            def wait(self, timeout=None):
                if self.behavior == "stuck":
                    raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
                return 0

            def kill(self):
                pass

        if proc_behavior == "absent":
            fake_proc = None
        else:
            fake_proc = FakeProc(proc_behavior)

        if clean_logout_result is None:
            clean_logout_result = (
                False, "failed_unreachable",
                "test stub: force SIGTERM fallback")

        if state is None:
            state = gc.State.MONITORING

        with patch.object(gc, "GATEWAY_PROC", fake_proc), \
             patch.object(gc, "gateway_proc", fake_proc), \
             patch.object(gc, "_current_state", state), \
             patch.object(gc, "_attempt_state_aware_clean_logout",
                          return_value=clean_logout_result), \
             patch.object(gc, "READY_FILE", "/tmp/nonexistent-ready-file"), \
             patch.object(gc.log, "info",
                          side_effect=lambda msg: info_calls.append(msg)), \
             patch.object(gc.log, "warning"), \
             patch("os.unlink"), \
             patch("sys.exit") as fake_exit:
            gc.shutdown(signum, None)
            fake_exit.assert_called_once_with(0)
        return info_calls

    def _find_alert(self, info_calls):
        hits = [m for m in info_calls if m.startswith("ALERT_SHUTDOWN ")]
        self.assertEqual(
            len(hits), 1,
            f"expected exactly one ALERT_SHUTDOWN line, got {len(hits)}: {info_calls!r}")
        return hits[0]

    def test_sigterm_clean_shutdown_emits_graceful_true(self):
        import signal as _signal
        calls = self._run_shutdown(_signal.SIGTERM, proc_behavior="clean")
        alert = self._find_alert(calls)
        self.assertIn("signal=SIGTERM", alert)
        self.assertIn("graceful=true", alert)
        self.assertIn(f"mode={gc.TRADING_MODE}", alert)
        self.assertIn('reason="', alert)

    def test_sigint_clean_shutdown_emits_graceful_true(self):
        import signal as _signal
        calls = self._run_shutdown(_signal.SIGINT, proc_behavior="clean")
        alert = self._find_alert(calls)
        self.assertIn("signal=SIGINT", alert)
        self.assertIn("graceful=true", alert)

    def test_stuck_jvm_emits_graceful_false(self):
        import signal as _signal
        calls = self._run_shutdown(_signal.SIGTERM, proc_behavior="stuck")
        alert = self._find_alert(calls)
        self.assertIn("graceful=false", alert)
        self.assertIn("SIGKILL", alert,
                      "graceful=false reason should mention SIGKILL for operator grep-ability")

    def test_no_gateway_proc_still_emits_graceful_true(self):
        # Controller can get SIGTERM before Gateway ever launches
        # (e.g. immediate Docker stop during image boot). ALERT_SHUTDOWN
        # must still fire so monitors see the lifecycle event.
        import signal as _signal
        calls = self._run_shutdown(_signal.SIGTERM, proc_behavior="absent")
        alert = self._find_alert(calls)
        self.assertIn("graceful=true", alert)

    def test_alert_shape_has_documented_keys_in_order(self):
        import signal as _signal
        calls = self._run_shutdown(_signal.SIGTERM, proc_behavior="clean")
        alert = self._find_alert(calls)
        # Keys appear in the order docs/OBSERVABILITY.md advertises —
        # mode, signal, graceful, reason — so grep-based extractors
        # that assume positional order don't break silently.
        mode_idx = alert.index("mode=")
        signal_idx = alert.index("signal=")
        graceful_idx = alert.index("graceful=")
        reason_idx = alert.index('reason="')
        self.assertLess(mode_idx, signal_idx)
        self.assertLess(signal_idx, graceful_idx)
        self.assertLess(graceful_idx, reason_idx)

    def test_clean_logout_success_skips_sigterm(self):
        """v0.5.6: when clean UI logout succeeds, shutdown() emits
        ALERT_CLEAN_LOGOUT status=succeeded AND ALERT_SHUTDOWN with the
        'via clean UI logout' wording, and does NOT call proc.terminate.
        """
        import signal as _signal

        clean_result = (True, "succeeded",
                        "JVM exited cleanly within 15s of WINDOW_CLOSING")
        calls = self._run_shutdown(
            _signal.SIGTERM, proc_behavior="clean",
            clean_logout_result=clean_result)

        # ALERT_CLEAN_LOGOUT fires with status=succeeded.
        logout_hits = [m for m in calls if m.startswith("ALERT_CLEAN_LOGOUT ")]
        self.assertEqual(len(logout_hits), 1)
        self.assertIn("status=succeeded", logout_hits[0])
        self.assertIn(f"mode={gc.TRADING_MODE}", logout_hits[0])

        # ALERT_SHUTDOWN still fires (lifecycle signal), graceful=true,
        # reason attributes the exit to the clean UI logout.
        alert = self._find_alert(calls)
        self.assertIn("graceful=true", alert)
        self.assertIn("clean UI logout", alert)
        self.assertIn("WINDOW_CLOSING", alert)

    def test_clean_logout_failure_falls_back_to_sigterm(self):
        """v0.5.6: when clean UI logout fails (agent unreachable), shutdown()
        emits ALERT_CLEAN_LOGOUT status=failed_* and still fires the old
        SIGTERM path, so ALERT_SHUTDOWN graceful=true still appears."""
        import signal as _signal

        clean_result = (False, "failed_unreachable",
                        "agent CLOSE_WIN did not succeed")
        calls = self._run_shutdown(
            _signal.SIGTERM, proc_behavior="clean",
            clean_logout_result=clean_result)

        logout_hits = [m for m in calls if m.startswith("ALERT_CLEAN_LOGOUT ")]
        self.assertEqual(len(logout_hits), 1)
        self.assertIn("status=failed_unreachable", logout_hits[0])

        alert = self._find_alert(calls)
        # SIGTERM path ran because clean_logout returned failure; the
        # fake proc.wait() succeeds so graceful stays true and the
        # reason is the existing "exited cleanly within 15s" wording.
        self.assertIn("graceful=true", alert)
        self.assertIn("exited cleanly within 15s", alert)

    def test_clean_logout_timeout_then_sigkill_emits_graceful_false(self):
        """v0.5.6: clean-logout timeout → SIGTERM → still stuck → SIGKILL.
        This is the worst-case compound failure path: UI close didn't
        work AND SIGTERM didn't work. Must still produce a usable
        ALERT_SHUTDOWN with graceful=false so operators see it."""
        import signal as _signal

        clean_result = (False, "failed_timeout",
                        "JVM still alive 15s after WINDOW_CLOSING")
        calls = self._run_shutdown(
            _signal.SIGTERM, proc_behavior="stuck",
            clean_logout_result=clean_result)

        logout_hits = [m for m in calls if m.startswith("ALERT_CLEAN_LOGOUT ")]
        self.assertEqual(len(logout_hits), 1)
        self.assertIn("status=failed_timeout", logout_hits[0])

        alert = self._find_alert(calls)
        self.assertIn("graceful=false", alert)
        self.assertIn("SIGKILL", alert)


class TestStateAwareShutdown(unittest.TestCase):
    """v0.5.9: SIGTERM / SIGINT during pre-MONITORING states emits a
    distinct ALERT_CLEAN_LOGOUT status label instead of falling through
    to v0.5.6's ``failed_unreachable`` (which was misleading — the
    agent wasn't unreachable, the main window just didn't exist yet).

    The status-label contract:
      INIT/LAUNCHING/AGENT_WAIT/APP_DISCOVERY/LOGIN → safe_no_session
      POST_LOGIN                                   → zombie_slot_cannot_release
      TWO_FA                                       → cancelled_pending_2fa / failed_cancel_2fa
      DISCLAIMERS…MONITORING                       → v0.5.6 monitoring path
    """

    def _run_shutdown_in_state(self, state, proc_behavior="clean",
                               clean_logout_result=None):
        helper = TestShutdownAlert()
        import signal as _signal
        return helper._run_shutdown(
            _signal.SIGTERM,
            proc_behavior=proc_behavior,
            clean_logout_result=clean_logout_result,
            state=state,
        )

    def _get_clean_logout_line(self, calls):
        hits = [m for m in calls if m.startswith("ALERT_CLEAN_LOGOUT ")]
        self.assertEqual(
            len(hits), 1,
            f"expected exactly one ALERT_CLEAN_LOGOUT, got {len(hits)}: "
            f"{calls!r}")
        return hits[0]

    def test_init_state_emits_safe_no_session(self):
        calls = self._run_shutdown_in_state(gc.State.INIT)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)
        # The reason must record which state we were in so operators
        # can tell "no JVM yet" from "auth not yet clicked" in logs.
        self.assertIn("state=INIT", line)

    def test_launching_state_emits_safe_no_session(self):
        calls = self._run_shutdown_in_state(gc.State.LAUNCHING)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)
        self.assertIn("state=LAUNCHING", line)

    def test_agent_wait_state_emits_safe_no_session(self):
        calls = self._run_shutdown_in_state(gc.State.AGENT_WAIT)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)

    def test_app_discovery_state_emits_safe_no_session(self):
        calls = self._run_shutdown_in_state(gc.State.APP_DISCOVERY)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)

    def test_login_state_emits_safe_no_session(self):
        calls = self._run_shutdown_in_state(gc.State.LOGIN)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)

    def test_post_login_state_emits_zombie_slot_cannot_release(self):
        """POST_LOGIN is the honest label: we have a CCP slot in flight
        but Gateway has not yet shown a main window we can WINDOW_CLOSE.
        SIGTERM here strands the slot — monitoring needs to see that
        distinctly from 'safe' and from 'close attempted'."""
        calls = self._run_shutdown_in_state(gc.State.POST_LOGIN)
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=zombie_slot_cannot_release", line)
        self.assertIn("CCP slot in flight", line)
        self.assertIn("state=POST_LOGIN", line)

    def test_pre_auth_state_does_not_call_clean_logout(self):
        """Safe-no-session paths must skip _attempt_state_aware_clean_logout
        entirely — the v0.5.6 helper needs the main window which
        doesn't exist yet, so calling it would always return
        failed_unreachable and poison the grep pipeline."""
        called = []
        helper = TestShutdownAlert()
        import signal as _signal
        with patch.object(gc, "_attempt_state_aware_clean_logout",
                          side_effect=lambda _s: called.append("x") or (
                              False, "failed_unreachable", "")):
            helper._run_shutdown(
                _signal.SIGTERM, proc_behavior="clean",
                state=gc.State.INIT,
            )
        self.assertEqual(
            called, [],
            "_attempt_state_aware_clean_logout must not be called in "
            "INIT state")

    def test_monitoring_state_still_uses_v056_clean_logout(self):
        """MONITORING must delegate to _attempt_state_aware_clean_logout
        (which under the hood calls the v0.5.6 helper). This is the
        unchanged happy path from v0.5.6."""
        clean_result = (True, "succeeded",
                        "JVM exited cleanly within 15s of WINDOW_CLOSING")
        calls = self._run_shutdown_in_state(
            gc.State.MONITORING,
            clean_logout_result=clean_result,
        )
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=succeeded", line)

    def test_no_gateway_proc_emits_safe_no_session_regardless_of_state(self):
        """If GATEWAY_PROC is None, there's no JVM to close; the correct
        status is safe_no_session no matter what state the controller
        was notionally in. Covers the 'SIGTERM before launch_gateway'
        race as well as the already-exited case."""
        calls = self._run_shutdown_in_state(
            gc.State.MONITORING, proc_behavior="absent")
        line = self._get_clean_logout_line(calls)
        self.assertIn("status=safe_no_session", line)


class TestClassifyShutdownForState(unittest.TestCase):
    """v0.5.9: pure-logic mapping from State → (attempt_close, status,
    reason). Split out so the decision table is testable without
    running the signal handler."""

    def test_pre_auth_states_skip_close_attempt(self):
        for state in (gc.State.INIT, gc.State.LAUNCHING,
                      gc.State.AGENT_WAIT, gc.State.APP_DISCOVERY,
                      gc.State.LOGIN):
            attempt, status, _reason = gc._classify_shutdown_for_state(state)
            self.assertFalse(
                attempt,
                f"{state.value}: should NOT attempt clean logout "
                "(no slot held, no UI to close)")
            self.assertEqual(status, "safe_no_session")

    def test_post_login_does_not_attempt_but_flags_zombie(self):
        attempt, status, reason = gc._classify_shutdown_for_state(
            gc.State.POST_LOGIN)
        self.assertFalse(attempt)
        self.assertEqual(status, "zombie_slot_cannot_release")
        self.assertIn("CCP slot in flight", reason)

    def test_two_fa_attempts_close_with_cancellation_label(self):
        attempt, status, _reason = gc._classify_shutdown_for_state(
            gc.State.TWO_FA)
        self.assertTrue(attempt)
        self.assertEqual(status, "cancelled_pending_2fa")

    def test_monitoring_family_attempts_close(self):
        for state in (gc.State.DISCLAIMERS, gc.State.API_WAIT,
                      gc.State.CONFIG, gc.State.READY,
                      gc.State.COMMAND_SERVER, gc.State.MONITORING):
            attempt, _status, _reason = gc._classify_shutdown_for_state(state)
            self.assertTrue(
                attempt,
                f"{state.value}: should attempt clean logout "
                "(main window rendered; WINDOW_CLOSING can land)")


class TestAttemptStateAwareCleanLogout(unittest.TestCase):
    """v0.5.9: TWO_FA path closes the 2FA dialog via the agent before
    relying on the v0.5.6 main-window close. The status labels
    cancelled_pending_2fa / failed_cancel_2fa are part of the
    ALERT_CLEAN_LOGOUT grep-contract."""

    def _fake_proc(self, poll_returns):
        class FakeProc:
            def __init__(self, values):
                self._values = list(values)
                self.pid = 12345

            def poll(self):
                if len(self._values) > 1:
                    return self._values.pop(0)
                return self._values[0]
        return FakeProc(poll_returns)

    def test_two_fa_success_cancels_pending_auth(self):
        """Agent closes the 2FA dialog and JVM exits → cancelled_pending_2fa."""
        proc = self._fake_proc([None, 0])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "_CLEAN_LOGOUT_TIMEOUT_SECONDS", 5), \
             patch.object(gc, "agent_close_window",
                          return_value=True) as close:
            success, status, reason = gc._attempt_state_aware_clean_logout(
                gc.State.TWO_FA)
        self.assertTrue(success)
        self.assertEqual(status, "cancelled_pending_2fa")
        self.assertIn("2FA dialog closed", reason)
        close.assert_called_once_with("Second Factor")

    def test_two_fa_agent_rejects_returns_failed_cancel(self):
        proc = self._fake_proc([None])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window", return_value=False):
            success, status, reason = gc._attempt_state_aware_clean_logout(
                gc.State.TWO_FA)
        self.assertFalse(success)
        self.assertEqual(status, "failed_cancel_2fa")
        self.assertIn("falling back to SIGTERM", reason)

    def test_two_fa_timeout_returns_failed_cancel(self):
        """Agent accepts but JVM stays alive → failed_cancel_2fa, not
        the v0.5.6 failed_timeout (distinct grep label)."""
        proc = self._fake_proc([None])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "_CLEAN_LOGOUT_TIMEOUT_SECONDS", 1), \
             patch.object(gc, "agent_close_window", return_value=True):
            success, status, _ = gc._attempt_state_aware_clean_logout(
                gc.State.TWO_FA)
        self.assertFalse(success)
        self.assertEqual(status, "failed_cancel_2fa")

    def test_two_fa_jvm_already_exited(self):
        """Same race semantics as v0.5.6: if JVM exits between outer
        check and here, report success without dispatching CLOSE_WIN."""
        proc = self._fake_proc([0])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window") as close:
            success, status, _ = gc._attempt_state_aware_clean_logout(
                gc.State.TWO_FA)
        self.assertTrue(success)
        self.assertEqual(status, "cancelled_pending_2fa")
        close.assert_not_called()

    def test_monitoring_delegates_to_v056_helper(self):
        """Non-TWO_FA states should just delegate to _attempt_clean_logout
        unchanged — no behaviour change for the v0.5.6 happy path."""
        expected = (True, "succeeded", "JVM exited cleanly within 15s")
        with patch.object(gc, "_attempt_clean_logout",
                          return_value=expected) as inner:
            result = gc._attempt_state_aware_clean_logout(
                gc.State.MONITORING)
        self.assertEqual(result, expected)
        inner.assert_called_once_with()


class TestAttemptCleanLogout(unittest.TestCase):
    """v0.5.6: _attempt_clean_logout drives the UI-level close path
    instead of relying on JVM shutdown hooks. The three status values
    (succeeded / failed_unreachable / failed_timeout) are part of the
    ALERT_CLEAN_LOGOUT grep-contract, so the tests pin the mapping
    from agent behaviour → status."""

    def _fake_proc(self, poll_returns):
        """Return a FakeProc whose poll() walks a list of return values
        (one per call). Once exhausted, stays at the last value."""
        class FakeProc:
            def __init__(self, values):
                self._values = list(values)
                self.pid = 12345

            def poll(self):
                if len(self._values) > 1:
                    return self._values.pop(0)
                return self._values[0]
        return FakeProc(poll_returns)

    def test_succeeded_when_jvm_exits_within_timeout(self):
        """Agent accepts CLOSE_WIN, JVM exits on the second poll."""
        proc = self._fake_proc([None, None, 0])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window", return_value=True):
            success, status, reason = gc._attempt_clean_logout(timeout_seconds=5)
        self.assertTrue(success)
        self.assertEqual(status, "succeeded")
        self.assertIn("exited cleanly", reason)

    def test_failed_unreachable_when_agent_rejects(self):
        """Agent CLOSE_WIN returns False (socket missing, EDT stalled
        before we could post). No polling wait — we bail immediately so
        caller can SIGTERM promptly."""
        proc = self._fake_proc([None])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window", return_value=False):
            success, status, reason = gc._attempt_clean_logout(timeout_seconds=5)
        self.assertFalse(success)
        self.assertEqual(status, "failed_unreachable")
        self.assertIn("falling back to SIGTERM", reason)

    def test_failed_timeout_when_jvm_stays_alive(self):
        """Agent accepts CLOSE_WIN but JVM never exits — WindowListener
        is stalled. Caller falls back to SIGTERM."""
        proc = self._fake_proc([None])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window", return_value=True):
            success, status, reason = gc._attempt_clean_logout(timeout_seconds=1)
        self.assertFalse(success)
        self.assertEqual(status, "failed_timeout")
        self.assertIn("still alive", reason)

    def test_jvm_already_exited_reports_succeeded_without_agent_call(self):
        """If the JVM exited on its own between the outer check and
        here, we report success without dispatching CLOSE_WIN."""
        proc = self._fake_proc([0])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "agent_close_window") as fake_close:
            success, status, reason = gc._attempt_clean_logout(timeout_seconds=5)
        self.assertTrue(success)
        self.assertEqual(status, "succeeded")
        self.assertIn("already exited", reason)
        fake_close.assert_not_called()

    def test_timeout_respects_env_default(self):
        """When timeout_seconds is None, uses _CLEAN_LOGOUT_TIMEOUT_SECONDS."""
        proc = self._fake_proc([None])
        with patch.object(gc, "GATEWAY_PROC", proc), \
             patch.object(gc, "_CLEAN_LOGOUT_TIMEOUT_SECONDS", 1), \
             patch.object(gc, "agent_close_window", return_value=True):
            success, status, _ = gc._attempt_clean_logout()
        self.assertFalse(success)
        self.assertEqual(status, "failed_timeout")


class TestAdaptiveCooldown(unittest.TestCase):
    """v0.5.5: CCP long cool-down scales with restart-attempt index.

    Pins the scaling curve so a refactor can't silently revert to the
    fixed-duration behaviour. That fixed 1200s was enough for IBKR's
    rate limiter but not long enough to outlast a stranded session slot
    from a prior unclean teardown — the root cause of the persistent
    lockout pattern (see memory/project_ccp_concurrent_session.md).
    """

    def test_attempt_1_returns_base(self):
        self.assertEqual(gc._compute_adaptive_cooldown(1, 1200, 1.5, 3600), 1200)

    def test_attempt_2_scales_by_multiplier(self):
        self.assertEqual(gc._compute_adaptive_cooldown(2, 1200, 1.5, 3600), 1800)

    def test_attempt_3_scales_again(self):
        self.assertEqual(gc._compute_adaptive_cooldown(3, 1200, 1.5, 3600), 2700)

    def test_caps_at_max(self):
        # 1200 * 1.5^10 = ~69k, clamped to 3600.
        self.assertEqual(gc._compute_adaptive_cooldown(11, 1200, 1.5, 3600), 3600)

    def test_multiplier_1_restores_legacy_fixed_behaviour(self):
        # Opt-out env for operators who prefer the pre-v0.5.5 curve.
        for attempt in range(1, 6):
            self.assertEqual(
                gc._compute_adaptive_cooldown(attempt, 1200, 1.0, 3600),
                1200,
                f"attempt={attempt} with mult=1.0 should stay at base")

    def test_nonpositive_attempt_treated_as_base(self):
        # Defensive: the docstring promises attempt <= 0 == 1.
        self.assertEqual(gc._compute_adaptive_cooldown(0, 1200, 1.5, 3600), 1200)
        self.assertEqual(gc._compute_adaptive_cooldown(-3, 1200, 1.5, 3600), 1200)

    def test_return_is_int(self):
        # time.sleep accepts float, but the log line reads better with an
        # int and operators grep on round-number durations.
        self.assertIsInstance(gc._compute_adaptive_cooldown(2, 1200, 1.5, 3600), int)


class TestUncleanShutdownAlert(unittest.TestCase):
    """v0.5.5: _teardown_jvm_for_restart() emits ALERT_JVM_UNCLEAN_SHUTDOWN
    when SIGKILL is required, so operators can see when a restart likely
    stranded an IBKR session slot."""

    class _FakeProc:
        def __init__(self, behavior):
            self.behavior = behavior  # "clean" | "stuck" | "terminate_raises"
            self.pid = 12345
            self._killed = False

        def poll(self):
            return None  # alive at teardown entry

        def terminate(self):
            if self.behavior == "terminate_raises":
                raise OSError("simulated terminate failure")

        def wait(self, timeout=None):
            if self.behavior == "stuck" and not self._killed:
                import subprocess
                raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
            return 0

        def kill(self):
            self._killed = True

    def _run_teardown(self, behavior, clean_logout_result=None):
        """Run _teardown_jvm_for_restart with a FakeProc of ``behavior``.

        ``clean_logout_result`` defaults to failure so the existing SIGTERM
        path is exercised; override to test the v0.5.6 success path."""
        if clean_logout_result is None:
            clean_logout_result = (
                False, "failed_unreachable",
                "test stub: force SIGTERM fallback")
        warning_calls = []
        info_calls = []
        fake = self._FakeProc(behavior)
        with patch.object(gc, "GATEWAY_PROC", fake), \
             patch.object(gc, "_attempt_clean_logout",
                          return_value=clean_logout_result), \
             patch.object(gc.log, "warning",
                          side_effect=lambda msg: warning_calls.append(msg)), \
             patch.object(gc.log, "info",
                          side_effect=lambda msg: info_calls.append(msg)), \
             patch.object(gc.log, "error"), \
             patch("os.unlink"):
            gc._teardown_jvm_for_restart()
        return warning_calls, info_calls

    def test_clean_teardown_does_not_emit_alert(self):
        warnings, _ = self._run_teardown("clean")
        alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(
            alerts, [],
            f"clean teardown should not emit ALERT_JVM_UNCLEAN_SHUTDOWN, got {warnings!r}")

    def test_sigkill_required_emits_alert(self):
        warnings, _ = self._run_teardown("stuck")
        alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(len(alerts), 1,
                         f"expected exactly one ALERT_JVM_UNCLEAN_SHUTDOWN, got {warnings!r}")
        alert = alerts[0]
        # Grep-contract pins:
        self.assertIn(f"mode={gc.TRADING_MODE}", alert)
        self.assertIn("pid=12345", alert)
        self.assertIn('reason="', alert)
        self.assertIn("SIGKILL", alert,
                      "reason should mention SIGKILL for operator grep")
        self.assertIn('implication="', alert,
                      "implication= field documents the suspected consequence")

    def test_terminate_exception_emits_alert(self):
        # Defensive path: if terminate() itself raises, the teardown
        # log captures it AND we still emit the ALERT so the stranded
        # session hypothesis is visible in the log trail.
        warnings, _ = self._run_teardown("terminate_raises")
        alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(len(alerts), 1)
        self.assertIn("OSError", alerts[0])

    def test_clean_logout_success_skips_sigterm_path(self):
        """v0.5.6: when clean logout succeeds, teardown emits
        ALERT_CLEAN_LOGOUT status=succeeded and does NOT emit
        ALERT_JVM_UNCLEAN_SHUTDOWN, even if the FakeProc is configured
        to be stuck — because terminate() is never called."""
        clean_result = (True, "succeeded",
                        "JVM exited cleanly within 15s of WINDOW_CLOSING")
        warnings, info = self._run_teardown(
            "stuck", clean_logout_result=clean_result)
        unclean_alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(
            unclean_alerts, [],
            f"clean logout success should skip SIGTERM entirely, got {warnings!r}")
        logout_alerts = [m for m in info if m.startswith("ALERT_CLEAN_LOGOUT ")]
        self.assertEqual(len(logout_alerts), 1)
        self.assertIn("status=succeeded", logout_alerts[0])
        self.assertIn(f"mode={gc.TRADING_MODE}", logout_alerts[0])
        self.assertIn("pid=12345", logout_alerts[0])

    def test_clean_logout_failure_emits_alert_and_falls_through(self):
        """v0.5.6: clean logout failure emits ALERT_CLEAN_LOGOUT status=
        failed_* AND continues to the SIGTERM path. With a stuck JVM,
        both ALERT_CLEAN_LOGOUT and ALERT_JVM_UNCLEAN_SHUTDOWN should
        appear — showing operators the full compound-failure picture."""
        clean_result = (False, "failed_timeout",
                        "JVM still alive 15s after WINDOW_CLOSING")
        warnings, info = self._run_teardown(
            "stuck", clean_logout_result=clean_result)
        logout_alerts = [m for m in info if m.startswith("ALERT_CLEAN_LOGOUT ")]
        unclean_alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(len(logout_alerts), 1)
        self.assertIn("status=failed_timeout", logout_alerts[0])
        self.assertEqual(len(unclean_alerts), 1,
                         "SIGTERM fallback still runs on clean-logout failure")


class TestIBKRMaintenanceWindow(unittest.TestCase):
    """v0.5.10: IBKR's daily server-side maintenance window (published
    23:45-00:15 ET) forcibly shuts down every Gateway session with exit
    code 0. Our recovery logic detects the window (widened to 23:30-00:30
    ET for safety) via wallclock and delays re-auth so IBKR's auth server
    can finish draining the prior session before we try again.

    These tests confirm the window predicate is correct across the
    boundary — including the midnight cross (t >= 23:30 OR t < 00:30) —
    so both the cold-start guard and the mid-run code-0 recovery path
    behave consistently.
    """

    def _et(self, hour, minute=0):
        """Build a tz-aware datetime at hour:minute America/New_York on
        an arbitrary date. Date choice is immaterial — the predicate
        only reads the time-of-day component."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        return datetime(2026, 4, 21, hour, minute,
                        tzinfo=ZoneInfo("America/New_York"))

    def test_at_23_30_is_in_window_lower_boundary(self):
        self.assertTrue(gc._is_ibkr_maintenance_window(self._et(23, 30)))

    def test_at_23_29_is_outside_window(self):
        self.assertFalse(gc._is_ibkr_maintenance_window(self._et(23, 29)))

    def test_at_23_46_is_in_window_matches_incident(self):
        # 2026-04-20/21 incident: JVM exited code 0 at 23:45 ET and
        # re-auth'd 8s later → CCP LOCKOUT. This is the canonical case.
        self.assertTrue(gc._is_ibkr_maintenance_window(self._et(23, 46)))

    def test_at_00_00_is_in_window_crosses_midnight(self):
        self.assertTrue(gc._is_ibkr_maintenance_window(self._et(0, 0)))

    def test_at_00_15_is_in_window_ibkr_published_end(self):
        self.assertTrue(gc._is_ibkr_maintenance_window(self._et(0, 15)))

    def test_at_00_29_is_in_window_near_upper_boundary(self):
        self.assertTrue(gc._is_ibkr_maintenance_window(self._et(0, 29)))

    def test_at_00_30_is_outside_window_upper_boundary_exclusive(self):
        self.assertFalse(gc._is_ibkr_maintenance_window(self._et(0, 30)))

    def test_at_noon_is_outside_window(self):
        self.assertFalse(gc._is_ibkr_maintenance_window(self._et(12, 0)))

    def test_at_evening_is_outside_window(self):
        self.assertFalse(gc._is_ibkr_maintenance_window(self._et(18, 0)))


class TestMaintenanceRecoveryDelay(unittest.TestCase):
    """v0.5.10: the delay itself is the mitigation. Verify the default
    duration, env-var-driven override (via the module-level constant),
    and that the delay helper emits the stable ALERT token so operators
    can distinguish this benign delay from a real CCP cascade."""

    def test_default_is_480_seconds_eight_minutes(self):
        self.assertEqual(gc._CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS_DEFAULT, 480)

    def test_apply_delay_sleeps_configured_duration(self):
        with patch("gateway_controller.time.sleep") as mock_sleep:
            gc._apply_maintenance_recovery_delay("test reason")
        mock_sleep.assert_called_once_with(
            gc._CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS)

    def test_apply_delay_emits_alert_token_with_fields(self):
        with self.assertLogs("controller", level="INFO") as cm:
            with patch("gateway_controller.time.sleep"):
                gc._apply_maintenance_recovery_delay("code-0 in window")
        combined = "\n".join(cm.output)
        self.assertIn("ALERT_IBKR_MAINTENANCE_RECOVERY", combined)
        self.assertIn(
            f"delay_seconds={gc._CCP_MAINTENANCE_RECOVERY_DELAY_SECONDS}",
            combined)
        self.assertIn(f"mode={gc.TRADING_MODE}", combined)
        self.assertIn('reason="code-0 in window"', combined)


class TestRecoverJvmMaintenanceGuard(unittest.TestCase):
    """v0.5.10: ``_recover_jvm_or_escalate`` must call the delay helper
    BEFORE attempting the fast restart when ``exit_code == 0`` and we're
    inside the maintenance window. At any other time (non-zero exit, or
    code 0 outside the window) the guard must not fire — code-0 exits
    outside the window are typically IBKR session kicks or auto-logoffs
    and benefit from the fast-restart path.

    We patch ``do_restart_in_place`` to return True so the function
    returns on the happy path without needing to construct a full JVM
    recovery environment.
    """

    def setUp(self):
        # These test the v0.5.10 guard, not issue #23 adoption. Without
        # this patch each one runs the real adoption path first and pays
        # its full detection budget (grace + socket probe) on a host that
        # has neither a restarter.log nor an agent socket.
        p = patch.object(gc, "_adopt_self_restarted_gateway",
                         return_value=False)
        p.start()
        self.addCleanup(p.stop)

    def test_code_0_in_window_calls_delay_before_restart(self):
        call_order = []
        with patch("gateway_controller._is_ibkr_maintenance_window",
                   return_value=True), \
             patch("gateway_controller._apply_maintenance_recovery_delay",
                   side_effect=lambda r: call_order.append("delay")) as mock_delay, \
             patch("gateway_controller.do_restart_in_place",
                   side_effect=lambda: (call_order.append("restart"), True)[1]):
            result = gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0)
        self.assertTrue(result)
        self.assertEqual(call_order, ["delay", "restart"])
        mock_delay.assert_called_once()

    def test_code_0_outside_window_skips_delay(self):
        with patch("gateway_controller._is_ibkr_maintenance_window",
                   return_value=False), \
             patch("gateway_controller._apply_maintenance_recovery_delay") as mock_delay, \
             patch("gateway_controller.do_restart_in_place",
                   return_value=True):
            gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0)
        mock_delay.assert_not_called()

    def test_non_zero_exit_skips_delay_even_in_window(self):
        # Non-zero exit codes are crashes, not IBKR cooperative
        # shutdowns. Even if the wallclock happens to fall inside the
        # maintenance window, the guard must not apply — fast restart
        # is still the right response.
        with patch("gateway_controller._is_ibkr_maintenance_window",
                   return_value=True), \
             patch("gateway_controller._apply_maintenance_recovery_delay") as mock_delay, \
             patch("gateway_controller.do_restart_in_place",
                   return_value=True):
            gc._recover_jvm_or_escalate(
                "JVM exited with code 143", exit_code=143)
        mock_delay.assert_not_called()

    def test_no_exit_code_preserves_pre_v0_5_10_behaviour(self):
        # Callers that don't pass exit_code (none exist in the current
        # code base but the kwarg defaults to None for future-proofing)
        # should never trigger the guard. Equivalent to "we don't know
        # if this was a maintenance exit, so don't delay".
        with patch("gateway_controller._is_ibkr_maintenance_window",
                   return_value=True), \
             patch("gateway_controller._apply_maintenance_recovery_delay") as mock_delay, \
             patch("gateway_controller.do_restart_in_place",
                   return_value=True):
            gc._recover_jvm_or_escalate("unspecified")
        mock_delay.assert_not_called()


class TestResolveTwofaDevice(unittest.TestCase):
    def test_explicit_wins(self):
        self.assertEqual(gc._resolve_twofa_device("IB Key", True), "IB Key")
        self.assertEqual(
            gc._resolve_twofa_device("  Mobile Authenticator app  ", False),
            "Mobile Authenticator app")

    def test_default_totp(self):
        self.assertEqual(gc._resolve_twofa_device("", True),
                         "Mobile Authenticator app")
        self.assertEqual(gc._resolve_twofa_device(None, True),
                         "Mobile Authenticator app")

    def test_default_non_totp(self):
        self.assertEqual(gc._resolve_twofa_device("", False), "IB Key")
        self.assertEqual(gc._resolve_twofa_device("   ", False), "IB Key")


class TestTwofaRequestedMethod(unittest.TestCase):
    def test_finds_enter_method_code_label(self):
        labels = [("Second Factor Authentication", "Some heading"),
                  ("Second Factor Authentication", "Enter Mobile Authenticator app code")]
        self.assertEqual(gc._twofa_requested_method(labels),
                         "Enter Mobile Authenticator app code")

    def test_none_when_no_prompt(self):
        labels = [("Second Factor Authentication", "Please authenticate"),
                  ("Second Factor Authentication", "OK")]
        self.assertIsNone(gc._twofa_requested_method(labels))

    def test_none_on_empty(self):
        self.assertIsNone(gc._twofa_requested_method([]))

    def test_window_scoped_extraction_from_full_label_set(self):
        # Regression for v0.7.0-rc1: agent_labels() returns labels from
        # ALL windows; we must scope by window TITLE (not by passing a
        # title substring to agent_labels, which filters by label text
        # and silently dropped the prompt). Given the realistic full set,
        # window-scoping must still find the 2FA prompt.
        full = [
            ("IBKR Gateway", "Connecting to server"),
            ("IBKR Gateway", "Account"),
            ("Second Factor Authentication", "Enter Mobile Authenticator app code"),
            ("Second Factor Authentication", "OK"),
        ]
        self.assertEqual(
            gc._twofa_requested_method(full, window_substr="Second Factor"),
            "Enter Mobile Authenticator app code")

    def test_window_scope_excludes_other_windows(self):
        # An "Enter ... code" label in some OTHER window must not be
        # picked when scoping to the 2FA window.
        labels = [("Some Other Dialog", "Enter card code")]
        self.assertIsNone(
            gc._twofa_requested_method(labels, window_substr="Second Factor"))


class TestTwofaMethodMismatch(unittest.TestCase):
    def test_match_is_not_mismatch(self):
        self.assertFalse(gc._twofa_method_mismatch(
            "Enter Mobile Authenticator app code", "Mobile Authenticator app"))

    def test_positive_mismatch(self):
        # Dialog wants IB Key, we can only do Mobile Authenticator (TOTP).
        self.assertTrue(gc._twofa_method_mismatch(
            "Enter IB Key code", "Mobile Authenticator app"))

    def test_lenient_when_no_prompt(self):
        # Unknown/absent prompt → never block (no regression for
        # single-method or unrecognized dialogs).
        self.assertFalse(gc._twofa_method_mismatch(None, "Mobile Authenticator app"))
        self.assertFalse(gc._twofa_method_mismatch("", "Mobile Authenticator app"))

    def test_lenient_when_no_desired(self):
        self.assertFalse(gc._twofa_method_mismatch("Enter IB Key code", ""))

    def test_head_token_match(self):
        # Env value slightly differs from the dialog wording but the
        # distinctive head token ("mobile authenticator") still matches.
        self.assertFalse(gc._twofa_method_mismatch(
            "Enter Mobile Authenticator app code", "Mobile Authenticator"))


class TestTwofaSelectorPresent(unittest.TestCase):
    # Dumps modeled on the agent's real WINDOW output for the two
    # account-dependent shapes of the Second Factor dialog (issue #20
    # ground truth; harness-verified 2026-08-14).

    SELECTOR_DUMP = (
        "=== window=Second Factor Authentication type=JDialog modal=true ===\n"
        "JDialog accName=\"Second Factor Authentication\"\n"
        "  JRootPane\n"
        "    JPanel\n"
        "      JTextArea text=\"Select second factor device\"\n"
        "      JScrollPane\n"
        "        JViewport\n"
        "          JList\n"
        "      JPanel\n"
        "        JButton text=\"OK\"\n"
        "        JButton text=\"Cancel\"\n"
        "        JButton text=\"Help\"\n"
    )

    LINK_DUMP = (
        "=== window=Second Factor Authentication type=JDialog modal=true ===\n"
        "JDialog accName=\"Second Factor Authentication\"\n"
        "  JRootPane\n"
        "    JPanel\n"
        "      JLabel text=\"Enter Mobile Authenticator app code\"\n"
        "      JTextField\n"
        "      JLabel text=\"Change input method\" hidden\n"
        "      JButton text=\"OK\"\n"
        "      JButton text=\"Cancel\"\n"
    )

    def test_detects_selector_variant(self):
        self.assertTrue(gc._twofa_selector_present(self.SELECTOR_DUMP))

    def test_link_variant_is_not_selector(self):
        # The pre-defaulted code dialog (issue #7 spike shape) must NOT
        # trigger the selector path — it goes straight to the v0.7.0
        # method-prompt check.
        self.assertFalse(gc._twofa_selector_present(self.LINK_DUMP))

    def test_agent_error_paths_are_lenient(self):
        # agent_window() returns "" on socket errors; never treat that
        # as a selector.
        self.assertFalse(gc._twofa_selector_present(""))
        self.assertFalse(gc._twofa_selector_present(None))


class TestHandle2faSelectorFlow(unittest.TestCase):
    """Orchestration tests for handle_2fa's device-selector path
    (#20/#21), with every agent_* wrapper stubbed at the module
    boundary and time.sleep no-op'd. The scenarios mirror the
    mock-dialog harness runs that validated the merge (2026-08-14):
    switch rejected server-side → dedicated ALERT reason; switch
    accepted → full selector → code-entry → TOTP flow; no selector →
    the v0.7.0 flow untouched (leniency invariant).
    """

    TWOFA_WINDOWS = [("JDialog", "Second Factor Authentication", True)]
    PROMPT_LABELS = [("Second Factor Authentication",
                      "Enter Mobile Authenticator app code")]

    def _run(self, *, window_dump, labels, jlist_ok=True):
        """Drive handle_2fa with a canned dialog shape. Returns
        (result, mocks dict, captured ERROR log lines)."""
        mocks = {}
        with patch.object(gc, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"), \
             patch.object(gc, "is_api_port_open", return_value=False), \
             patch.object(gc, "agent_windows",
                          return_value=self.TWOFA_WINDOWS), \
             patch.object(gc, "agent_window",
                          return_value=window_dump), \
             patch.object(gc, "agent_labels", return_value=labels), \
             patch.object(gc, "agent_jlist_select",
                          return_value=jlist_ok) as jls, \
             patch.object(gc, "agent_settext_in_window",
                          return_value=True) as stw, \
             patch.object(gc, "agent_click_in_window",
                          return_value=True) as cw, \
             patch.object(gc, "generate_totp", return_value="123456"), \
             patch.object(gc.time, "sleep"):
            mocks["jlist"] = jls
            mocks["settext"] = stw
            mocks["click"] = cw
            with _capture_controller_errors() as errors:
                result = gc.handle_2fa(None)
        return result, mocks, errors

    def test_switch_rejected_fails_with_dedicated_reason(self):
        # Selector detected, selection + OK succeed, but no
        # "Enter <method> code" prompt ever appears (IBKR rejected the
        # switch server-side). Must fail loud with the dedicated
        # reason, and must NOT attempt to type the code.
        result, mocks, errors = self._run(
            window_dump=TestTwofaSelectorPresent.SELECTOR_DUMP,
            labels=[])
        self.assertFalse(result)
        self.assertTrue(any(
            'reason="2FA device switch produced no code-entry dialog"'
            in line for line in errors),
            f"dedicated ALERT reason missing from: {errors}")
        mocks["jlist"].assert_called_once_with(
            "Second Factor", "Mobile Authenticator app")
        mocks["settext"].assert_not_called()

    def test_switch_accepted_completes_totp_flow(self):
        # Selector detected and, after selection, the code-entry
        # prompt appears — the flow must continue through the normal
        # v0.7.0 prompt check and type the TOTP.
        result, mocks, errors = self._run(
            window_dump=TestTwofaSelectorPresent.SELECTOR_DUMP,
            labels=self.PROMPT_LABELS)
        self.assertTrue(result)
        mocks["jlist"].assert_called_once_with(
            "Second Factor", "Mobile Authenticator app")
        mocks["settext"].assert_called_once_with("Second Factor", "123456")
        # OK clicked twice: once on the selector, once on the code dialog.
        self.assertEqual(mocks["click"].call_count, 2)

    def test_jlist_failure_fails_loud(self):
        result, mocks, errors = self._run(
            window_dump=TestTwofaSelectorPresent.SELECTOR_DUMP,
            labels=[], jlist_ok=False)
        self.assertFalse(result)
        self.assertTrue(any(
            'reason="JLIST_SELECT on 2FA device selector failed"'
            in line for line in errors))
        mocks["settext"].assert_not_called()

    def test_no_selector_runs_v070_flow_untouched(self):
        # Link-variant dump (code dialog): JLIST_SELECT must never be
        # called and the pre-existing flow must succeed — the
        # no-regression guarantee for single-method and link-variant
        # accounts.
        result, mocks, errors = self._run(
            window_dump=TestTwofaSelectorPresent.LINK_DUMP,
            labels=self.PROMPT_LABELS)
        self.assertTrue(result)
        mocks["jlist"].assert_not_called()
        mocks["settext"].assert_called_once_with("Second Factor", "123456")
        self.assertEqual(mocks["click"].call_count, 1)


class TestDetectPasskeyFlow(unittest.TestCase):
    NORMAL_LOGIN = [
        ("JFrame", "IBKR Gateway", False),
        ("aV", "Second Factor Authentication", True),
    ]

    def test_matches_each_signature(self):
        for title in ("Passkey", "WebAuthn ceremony",
                      "Insert your Security Key", "JxBrowser"):
            with self.subTest(title=title):
                windows = [("JFrame", title, True)]
                self.assertIsNotNone(gc._detect_passkey_flow(windows))

    def test_case_insensitive(self):
        self.assertEqual(
            gc._detect_passkey_flow([("x", "USE YOUR PASSKEY", True)]),
            "passkey")

    def test_normal_login_is_not_passkey(self):
        self.assertIsNone(gc._detect_passkey_flow(self.NORMAL_LOGIN))

    def test_empty_and_none_title(self):
        self.assertIsNone(gc._detect_passkey_flow([]))
        self.assertIsNone(gc._detect_passkey_flow([("x", None, False)]))


class TestHandle2faPasskeyFlow(unittest.TestCase):
    PASSKEY_WINDOWS = [
        ("JFrame", "IBKR Gateway", False),
        ("aV", "Security Key Authentication", True),
    ]

    def test_passkey_window_fails_loud_without_typing(self):
        # A passkey ceremony window in the 2FA wait must fail with the
        # dedicated reason and never type a TOTP code (we don't drive
        # WebAuthn — that would mean holding the user's private key).
        with patch.object(gc, "TOTP_SECRET", "JBSWY3DPEHPK3PXP"), \
             patch.object(gc, "is_api_port_open", return_value=False), \
             patch.object(gc, "agent_windows",
                          return_value=self.PASSKEY_WINDOWS), \
             patch.object(gc, "agent_settext_in_window",
                          return_value=True) as stw, \
             patch.object(gc, "generate_totp", return_value="123456"), \
             patch.object(gc.time, "sleep"):
            with _capture_controller_errors() as errors:
                result = gc.handle_2fa(None)
        self.assertFalse(result)
        self.assertTrue(any(
            'reason="passkey/WebAuthn 2FA flow - unattended login '
            'not supported"' in line for line in errors),
            f"passkey ALERT reason missing from: {errors}")
        stw.assert_not_called()


class _capture_controller_errors:
    """Context manager collecting ERROR-level lines from the
    'controller' logger without failing when none are emitted
    (assertLogs raises on zero records; success paths emit none)."""

    def __enter__(self):
        import logging

        class _ListHandler(logging.Handler):
            def __init__(self, sink):
                super().__init__(level=logging.ERROR)
                self.sink = sink

            def emit(self, record):
                self.sink.append(record.getMessage())

        self.lines = []
        self.handler = _ListHandler(self.lines)
        logging.getLogger("controller").addHandler(self.handler)
        return self.lines

    def __exit__(self, *exc):
        import logging
        logging.getLogger("controller").removeHandler(self.handler)
        return False


# ── Issue #23: Gateway self-restart adoption ───────────────────────────

def _sleeper():
    """Spawn a throwaway child that idles until killed."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


class TestLoginButtonLabels(unittest.TestCase):
    """Gateway renames the Log In button per mode. Probing the wrong one
    first made every paper login emit a spurious ERROR-level
    `agent CLICK 'Log In': ERR not_found` immediately before succeeding
    — harmless, but it false-positives any "ERROR means page someone"
    rule. Found in the v0.9.0 pre-release spike."""

    def test_paper_tries_paper_label_first(self):
        self.assertEqual(gc._login_button_labels("paper"),
                         ("Paper Log In", "Log In"))

    def test_live_tries_plain_label_first(self):
        self.assertEqual(gc._login_button_labels("live"),
                         ("Log In", "Paper Log In"))

    def test_both_labels_always_offered(self):
        for mode in ("paper", "live"):
            self.assertEqual(set(gc._login_button_labels(mode)),
                             {"Log In", "Paper Log In"})

    def test_defaults_to_module_trading_mode(self):
        with patch.object(gc, "TRADING_MODE", "paper"):
            self.assertEqual(gc._login_button_labels()[0], "Paper Log In")
        with patch.object(gc, "TRADING_MODE", "live"):
            self.assertEqual(gc._login_button_labels()[0], "Log In")


class TestAgentClickQuiet(unittest.TestCase):
    """quiet=True demotes an expected miss to DEBUG so routine probes
    don't look like failures; real failures must stay at ERROR."""

    def test_failure_logs_error_by_default(self):
        with patch.object(gc, "_agent_request", return_value="ERR not_found"), \
             patch.object(gc.log, "error") as err, \
             patch.object(gc.log, "debug") as dbg:
            self.assertFalse(gc.agent_click("Log In"))
            err.assert_called_once()
            dbg.assert_not_called()

    def test_quiet_failure_logs_debug(self):
        with patch.object(gc, "_agent_request", return_value="ERR not_found"), \
             patch.object(gc.log, "error") as err, \
             patch.object(gc.log, "debug") as dbg:
            self.assertFalse(gc.agent_click("Log In", quiet=True))
            err.assert_not_called()
            dbg.assert_called_once()

    def test_quiet_exception_also_demoted(self):
        with patch.object(gc, "_agent_request", side_effect=OSError("boom")), \
             patch.object(gc.log, "error") as err, \
             patch.object(gc.log, "debug") as dbg:
            self.assertFalse(gc.agent_click("X", quiet=True))
            err.assert_not_called()
            dbg.assert_called_once()

    def test_success_never_logs(self):
        with patch.object(gc, "_agent_request", return_value="OK"), \
             patch.object(gc.log, "error") as err, \
             patch.object(gc.log, "debug") as dbg:
            self.assertTrue(gc.agent_click("Log In", quiet=True))
            err.assert_not_called()
            dbg.assert_not_called()


class TestInstall4jRestarterAge(unittest.TestCase):
    """_install4j_restarter_age is the only trigger for self-restart
    detection: seconds since install4j last wrote
    <launcher dir>/.install4j/restarter.log, or None when the file is
    missing / stale / the launcher is unknown."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = os.path.join(self.tmp.name, "ibgateway", "10.45.1j")
        os.makedirs(os.path.join(d, ".install4j"))
        self.launcher = os.path.join(d, "ibgateway")
        open(self.launcher, "w").close()
        self.log_path = os.path.join(d, ".install4j", "restarter.log")
        self._orig_launcher = gc._GATEWAY_LAUNCHER_PATH
        gc._GATEWAY_LAUNCHER_PATH = self.launcher

    def tearDown(self):
        gc._GATEWAY_LAUNCHER_PATH = self._orig_launcher
        self.tmp.cleanup()

    def _write_log(self, age_seconds=0):
        with open(self.log_path, "w") as f:
            f.write("[INFO] Finished\n")
        t = time.time() - age_seconds
        os.utime(self.log_path, (t, t))

    def test_log_path_sits_next_to_launcher(self):
        self.assertEqual(gc._install4j_restarter_log_path(), self.log_path)

    def test_missing_log_is_none(self):
        self.assertIsNone(gc._install4j_restarter_age())

    def test_fresh_log_reports_age(self):
        self._write_log(age_seconds=3)
        age = gc._install4j_restarter_age()
        self.assertIsNotNone(age)
        self.assertGreaterEqual(age, 0.0)
        self.assertLess(age, 10.0)

    def test_stale_log_is_none(self):
        # Yesterday's restart (or a file left on a persistent volume)
        # must not be mistaken for a restart in progress.
        self._write_log(age_seconds=gc._AUTO_RESTART_DETECT_WINDOW_SECONDS + 60)
        self.assertIsNone(gc._install4j_restarter_age())

    def test_future_mtime_clamps_to_zero(self):
        self._write_log(age_seconds=-30)
        self.assertEqual(gc._install4j_restarter_age(), 0.0)

    def test_far_future_log_is_none(self):
        # Clock skew between the container and a persistent settings
        # volume must not make every clean exit look like a restart.
        self._write_log(age_seconds=-(gc._AUTO_RESTART_DETECT_WINDOW_SECONDS + 60))
        self.assertIsNone(gc._install4j_restarter_age())

    def test_unknown_launcher_is_none(self):
        gc._GATEWAY_LAUNCHER_PATH = None
        with patch.object(gc, "find_gateway_launcher", return_value=None):
            self.assertIsNone(gc._install4j_restarter_log_path())
            self.assertIsNone(gc._install4j_restarter_age())

    def test_falls_back_to_launcher_discovery(self):
        gc._GATEWAY_LAUNCHER_PATH = None
        self._write_log()
        with patch.object(gc, "find_gateway_launcher",
                          return_value=self.launcher):
            self.assertIsNotNone(gc._install4j_restarter_age())


class TestInstall4jRestarterDiscrimination(unittest.TestCase):
    """install4j's restarter is itself a JVM that inherits
    INSTALL4J_ADD_VM_PARAMS, so it loads our agent and answers GET_PID
    with its own PID for the seconds it lives. Adopting it would mean
    monitoring a process that exits immediately. Both fixtures below are
    real cmdlines captured 2026-09-06 — the restarter from running the
    binary in a container off the release image, Gateway from the live
    container's JVM."""

    RESTARTER = (
        "/usr/local/zulu17.60.17-ca-fx-jre17.0.16-linux_aarch64/bin/java "
        "--add-opens java.desktop/java.awt=ALL-UNNAMED "
        "-Dinstall4j.alternativeLogfile=./.install4j/restarter.log "
        "-javaagent:/home/ibgateway/gateway-input-agent.jar=/tmp/probe.sock "
        "-Djava.security.manager=allow -classpath "
        "/home/ibgateway/Jts/ibgateway/10.45.1g/.install4j/i4jruntime.jar:"
        "/home/ibgateway/Jts/ibgateway/10.45.1g/.install4j/launcher3257f6f9.jar "
        "install4j.App1256852828Id640 640 11 /tmp/marker.sh ")

    GATEWAY = (
        "/usr/local/zulu17.60.17-ca-fx-jre17.0.16-linux_aarch64/bin/java "
        "-splash:/home/ibgateway/Jts/ibgateway/10.45.1g/.install4j/s_clyey9.png "
        "--add-opens=java.base/java.util=ALL-UNNAMED "
        "-javaagent:/home/ibgateway/gateway-input-agent.jar="
        "/tmp/gateway-input-live.sock -classpath "
        "/home/ibgateway/Jts/ibgateway/10.45.1g/.install4j/i4jruntime.jar "
        "-VjtsConfigDir=/home/ibgateway/Jts_live -VinstallerType=standalone ")

    def test_identifies_the_restarter(self):
        self.assertTrue(gc._cmdline_is_install4j_restarter(self.RESTARTER))

    def test_does_not_mistake_gateway_for_the_restarter(self):
        # Both carry i4jruntime.jar and our -javaagent, so neither is a
        # usable discriminator; only the restarter's own log flag is.
        self.assertFalse(gc._cmdline_is_install4j_restarter(self.GATEWAY))
        self.assertIn("i4jruntime.jar", self.GATEWAY)
        self.assertIn("gateway-input-agent.jar", self.GATEWAY)

    def test_unknown_cmdline_is_not_the_restarter(self):
        # No /proc (macOS) must read as "not the restarter", leaving the
        # adoption retry to cover it rather than refusing to adopt.
        self.assertFalse(gc._cmdline_is_install4j_restarter(None))
        self.assertFalse(gc._cmdline_is_install4j_restarter(""))

    def test_own_process_is_not_the_restarter(self):
        self.assertFalse(gc._is_install4j_restarter_pid(os.getpid()))

    def test_cmdline_of_a_missing_pid_is_none(self):
        self.assertIsNone(gc._process_cmdline(2 ** 22 + 12345))

    @unittest.skipUnless(os.path.isdir("/proc"), "needs Linux /proc")
    def test_reads_a_real_cmdline(self):
        self.assertIn("python", (gc._process_cmdline(os.getpid()) or "").lower())


class TestAdoptedProcess(unittest.TestCase):
    """_AdoptedProcess must look enough like subprocess.Popen for
    monitor_loop, _teardown_jvm_for_restart, shutdown() and the health
    snapshot: pid / poll() / returncode / terminate() / kill() / wait().
    The exit status of a non-child is not observable, so a gone process
    reports the _EXIT_STATUS_UNOBSERVABLE sentinel — never 0."""

    def test_live_process_polls_none(self):
        p = _sleeper()
        try:
            a = gc._AdoptedProcess(p.pid)
            self.assertEqual(a.pid, p.pid)
            self.assertIsNone(a.poll())
            self.assertIsNone(a.returncode)
        finally:
            p.kill()
            p.wait()

    def test_terminate_then_wait_reports_unobservable_exit(self):
        p = _sleeper()
        a = gc._AdoptedProcess(p.pid)
        a.terminate()
        p.wait(timeout=10)  # reap so it's not a zombie on Linux
        self.assertEqual(a.wait(timeout=5), gc._EXIT_STATUS_UNOBSERVABLE)
        self.assertEqual(a.poll(), gc._EXIT_STATUS_UNOBSERVABLE)
        self.assertIsNotNone(a.poll())
        self.assertNotEqual(a.returncode, 0)

    def test_wait_times_out_while_alive(self):
        p = _sleeper()
        try:
            a = gc._AdoptedProcess(p.pid)
            with self.assertRaises(subprocess.TimeoutExpired):
                a.wait(timeout=0.3)
        finally:
            p.kill()
            p.wait()

    def test_kill_is_honoured(self):
        p = _sleeper()
        a = gc._AdoptedProcess(p.pid)
        a.kill()
        p.wait(timeout=10)
        self.assertEqual(a.poll(), gc._EXIT_STATUS_UNOBSERVABLE)

    def test_signals_are_withheld_when_identity_no_longer_matches(self):
        # The adopted PID is not our child: between adoption and teardown
        # the kernel can recycle it. Signalling then would kill an
        # unrelated process.
        p = _sleeper()
        try:
            a = gc._AdoptedProcess(p.pid)
            with patch.object(gc._AdoptedProcess, "_alive", lambda self: False), \
                 patch.object(gc.os, "kill") as kill:
                a.terminate()
                a.kill()
                kill.assert_not_called()
        finally:
            p.kill()
            p.wait()

    def test_signals_to_gone_process_do_not_raise(self):
        p = _sleeper()
        pid = p.pid
        p.kill()
        p.wait()
        a = gc._AdoptedProcess(pid)
        a.terminate()
        a.kill()
        self.assertEqual(a.poll(), gc._EXIT_STATUS_UNOBSERVABLE)

    def test_unreaped_zombie_counts_as_exited(self):
        # In the container the self-restarted JVM is reparented to
        # run.sh; if its exit were ever left unreaped, os.kill(pid, 0)
        # would still succeed on the zombie. /proc's state must win —
        # and where there is no /proc, the WNOHANG reap must. Without
        # one of the two a dead JVM looks alive forever and teardown
        # burns its whole SIGTERM grace on it (caught by the end-to-end
        # adoption tests in test_core_logic.py).
        p = _sleeper()
        a = gc._AdoptedProcess(p.pid)
        p.kill()
        time.sleep(0.5)  # dead but deliberately not yet reaped
        try:
            self.assertEqual(a.poll(), gc._EXIT_STATUS_UNOBSERVABLE)
        finally:
            p.wait()

    @unittest.skipUnless(os.path.isdir("/proc"), "needs Linux /proc")
    def test_proc_stat_parses_state_and_starttime(self):
        stat = gc._proc_stat(os.getpid())
        self.assertIsNotNone(stat)
        state, starttime = stat
        self.assertIn(state, ("R", "S", "D"))
        self.assertIsInstance(starttime, int)

    def test_proc_stat_missing_is_none(self):
        self.assertIsNone(gc._proc_stat(2 ** 22 + 12345))


class TestAdoptSelfRestartedGateway(unittest.TestCase):
    """_adopt_self_restarted_gateway orchestration: only acts on a
    fresh restarter.log, waits for the NEW JVM's agent, repoints the
    globals before waiting on the API port, and reports True only when
    the adopted Gateway is serving (port open, or login re-driven)."""

    _GLOBALS = ("GATEWAY_PROC", "CURRENT_APP", "JVM_PID",
                "_command_server_app",
                "_AUTO_RESTART_ADOPT_TIMEOUT_SECONDS",
                "_AUTO_RESTART_API_TIMEOUT_SECONDS",
                "_AUTO_RESTART_DETECT_GRACE_SECONDS",
                "_AUTO_RESTART_PROBE_SECONDS")

    def setUp(self):
        self._saved = {k: getattr(gc, k) for k in self._GLOBALS}
        gc.GATEWAY_PROC = None
        gc.CURRENT_APP = None
        gc._command_server_app = None
        gc.JVM_PID = 27
        gc._AUTO_RESTART_ADOPT_TIMEOUT_SECONDS = 1
        gc._AUTO_RESTART_API_TIMEOUT_SECONDS = 2
        gc._AUTO_RESTART_DETECT_GRACE_SECONDS = 0
        # Off by default here so each test says explicitly whether it is
        # exercising the restarter.log path or the socket-probe fallback.
        gc._AUTO_RESTART_PROBE_SECONDS = 0

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(gc, k, v)

    def _adopting(self, **overrides):
        """Patch the collaborators for a successful adoption; override
        individual ones per test."""
        cfg = dict(
            age=1.0, new_pid=5020, alive=True, port_open=True,
            texts=set(), buttons=set(), redrive=True,
        )
        cfg.update(overrides)
        stack = [
            patch.object(gc, "_install4j_restarter_age", return_value=cfg["age"]),
            patch.object(gc, "_wait_for_self_restarted_agent",
                         return_value=cfg["new_pid"]),
            patch.object(gc._AdoptedProcess, "_alive", return_value=cfg["alive"]),
            patch.object(gc, "is_api_port_open", return_value=cfg["port_open"]),
            patch.object(gc, "agent_list",
                         return_value=(cfg["texts"], cfg["buttons"])),
            patch.object(gc, "agent_windows", return_value=[]),
            patch.object(gc, "agent_click", return_value=True),
            patch.object(gc, "_redrive_login", return_value=cfg["redrive"]),
            patch.object(gc, "signal_ready"),
        ]
        return stack

    def _run(self, stack, exit_code=0):
        mocks = {}
        for p in stack:
            m = p.start()
            self.addCleanup(p.stop)
            mocks[p.attribute] = m
        result = gc._adopt_self_restarted_gateway("JVM exited with code 0",
                                                   exit_code=exit_code)
        return result, mocks

    def test_not_a_self_restart_returns_false_untouched(self):
        # No restarter.log and the probe disabled: nothing to adopt.
        result, mocks = self._run(self._adopting(age=None))
        self.assertFalse(result)
        mocks["_wait_for_self_restarted_agent"].assert_not_called()
        self.assertIsNone(gc.GATEWAY_PROC)
        self.assertEqual(gc.JVM_PID, 27)

    def test_socket_probe_adopts_when_no_restarter_log_is_written(self):
        # Verified 2026-09-06 on Gateway 10.45.1g: the install4j restarter
        # ships but writes no restarter.log. A live JVM answering our
        # agent socket that we did not spawn is direct evidence of a
        # self-restart, so adoption must not depend on the log alone.
        gc._AUTO_RESTART_PROBE_SECONDS = 5
        result, mocks = self._run(self._adopting(age=None))
        self.assertTrue(result)
        self.assertEqual(gc.JVM_PID, 5020)
        # The probe result is reused; no second wait for the same JVM.
        wait = mocks["_wait_for_self_restarted_agent"]
        wait.assert_called_once()
        self.assertEqual(wait.call_args[0][:2], (27, 5))
        mocks["signal_ready"].assert_called_once()

    def test_restarter_on_the_socket_counts_as_detection(self):
        # The restarter answering our socket is conclusive evidence of a
        # self-restart even with no usable restarter.log — but it must
        # not itself be adopted; we wait for the Gateway JVM it launches.
        gc._AUTO_RESTART_PROBE_SECONDS = 5
        stack = self._adopting(age=None)

        def wait(old, timeout, exclude=(), seen_restarter=None):
            if seen_restarter is not None and not seen_restarter:
                seen_restarter.append(99)      # restarter seen, no Gateway yet
                return None
            return 5020                         # Gateway's JVM arrives

        stack[1] = patch.object(gc, "_wait_for_self_restarted_agent",
                                side_effect=wait)
        result, mocks = self._run(stack)
        self.assertTrue(result)
        self.assertEqual(gc.JVM_PID, 5020)
        self.assertEqual(mocks["_wait_for_self_restarted_agent"].call_count, 2)

    def test_socket_probe_finding_nothing_falls_through(self):
        gc._AUTO_RESTART_PROBE_SECONDS = 5
        result, _ = self._run(self._adopting(age=None, new_pid=None))
        self.assertFalse(result)
        self.assertIsNone(gc.GATEWAY_PROC)
        self.assertEqual(gc.JVM_PID, 27)

    def test_socket_probe_skipped_on_a_crash(self):
        # A non-zero exit is a crash, not a self-restart: the relaunch
        # path owns it and must not be delayed by the probe.
        gc._AUTO_RESTART_PROBE_SECONDS = 5
        result, mocks = self._run(self._adopting(age=None), exit_code=1)
        self.assertFalse(result)
        mocks["_wait_for_self_restarted_agent"].assert_not_called()

    def test_grace_recheck_catches_restarter_write_after_exit(self):
        # The restarter's first write can trail the code-0 exit by a
        # couple of seconds; a short re-check window covers it.
        gc._AUTO_RESTART_DETECT_GRACE_SECONDS = 3
        ages = iter([None, None, 1.0])
        stack = self._adopting()
        stack[0] = patch.object(gc, "_install4j_restarter_age",
                                side_effect=lambda: next(ages, 1.0))
        result, mocks = self._run(stack)
        self.assertTrue(result)
        self.assertGreaterEqual(mocks["_install4j_restarter_age"].call_count, 3)

    def test_no_grace_for_nonzero_exit(self):
        # A crash (non-zero code) is not a self-restart candidate;
        # don't spend the grace window on it.
        gc._AUTO_RESTART_DETECT_GRACE_SECONDS = 3
        result, mocks = self._run(self._adopting(age=None), exit_code=1)
        self.assertFalse(result)
        self.assertEqual(mocks["_install4j_restarter_age"].call_count, 1)

    def test_no_new_agent_falls_through_without_adopting(self):
        result, mocks = self._run(self._adopting(new_pid=None))
        self.assertFalse(result)
        self.assertIsNone(gc.GATEWAY_PROC)
        self.assertEqual(gc.JVM_PID, 27)
        mocks["signal_ready"].assert_not_called()

    def test_adopts_when_api_port_opens(self):
        result, mocks = self._run(self._adopting())
        self.assertTrue(result)
        self.assertIsInstance(gc.GATEWAY_PROC, gc._AdoptedProcess)
        self.assertEqual(gc.GATEWAY_PROC.pid, 5020)
        self.assertEqual(gc.JVM_PID, 5020)
        self.assertEqual(gc.CURRENT_APP.get_process_id(), 5020)
        self.assertIs(gc._command_server_app, gc.CURRENT_APP)
        mocks["signal_ready"].assert_called_once()
        mocks["_redrive_login"].assert_not_called()
        wait = mocks["_wait_for_self_restarted_agent"]
        wait.assert_called_once()
        self.assertEqual(wait.call_args[0][0], 27)      # old pid
        self.assertIn("exclude", wait.call_args[1])

    def test_login_dialog_means_session_not_preserved_and_is_driven(self):
        result, mocks = self._run(self._adopting(
            port_open=False, texts={"Username", "Password"}))
        self.assertTrue(result)
        mocks["_redrive_login"].assert_called_once()
        self.assertEqual(
            mocks["_redrive_login"].call_args[0][0].get_process_id(), 5020)
        # _redrive_login signals readiness itself; adoption must not
        # double-signal.
        mocks["signal_ready"].assert_not_called()

    def test_login_failure_falls_through_with_proc_repointed(self):
        result, mocks = self._run(self._adopting(
            port_open=False, texts={"Username"}, redrive=False))
        self.assertFalse(result)
        # The adopted instance is left as GATEWAY_PROC so the fallback's
        # teardown terminates it instead of no-op'ing on a dead Popen.
        self.assertIsInstance(gc.GATEWAY_PROC, gc._AdoptedProcess)
        self.assertEqual(gc.GATEWAY_PROC.pid, 5020)

    def test_api_timeout_falls_through_with_proc_repointed(self):
        result, mocks = self._run(self._adopting(port_open=False))
        self.assertFalse(result)
        self.assertIsInstance(gc.GATEWAY_PROC, gc._AdoptedProcess)
        mocks["signal_ready"].assert_not_called()

    def test_adopted_jvm_dying_falls_through(self):
        result, mocks = self._run(self._adopting(alive=False, port_open=True))
        self.assertFalse(result)
        mocks["signal_ready"].assert_not_called()

    def test_same_restarter_log_is_not_adopted_twice(self):
        # The log stays "fresh" for 120s. An adopted JVM that dies inside
        # that window is a failed restart, not a second self-restart —
        # going through the adoption wait again just delays recovery.
        saved = gc._LAST_ADOPTED_RESTART_MTIME
        try:
            stack = self._adopting()
            stack.append(patch.object(gc, "_install4j_restarter_mtime",
                                      return_value=1234.5))
            gc._LAST_ADOPTED_RESTART_MTIME = 1234.5
            result, mocks = self._run(stack)
            self.assertFalse(result)
            mocks["_wait_for_self_restarted_agent"].assert_not_called()
        finally:
            gc._LAST_ADOPTED_RESTART_MTIME = saved

    def test_records_restarter_mtime_when_it_acts(self):
        saved = gc._LAST_ADOPTED_RESTART_MTIME
        try:
            gc._LAST_ADOPTED_RESTART_MTIME = None
            stack = self._adopting()
            stack.append(patch.object(gc, "_install4j_restarter_mtime",
                                      return_value=999.0))
            result, _ = self._run(stack)
            self.assertTrue(result)
            self.assertEqual(gc._LAST_ADOPTED_RESTART_MTIME, 999.0)
        finally:
            gc._LAST_ADOPTED_RESTART_MTIME = saved

    def test_retries_when_first_candidate_dies(self):
        # install4j's restarter chain can briefly expose a JVM that isn't
        # the replacement Gateway. Falling straight through to a relaunch
        # on the first dead candidate would re-create the issue #23 race.
        gc._AUTO_RESTART_ADOPT_TIMEOUT_SECONDS = 8
        stack = self._adopting()
        stack[1] = patch.object(gc, "_wait_for_self_restarted_agent",
                                side_effect=[5020, 5021])
        stack[2] = patch.object(gc._AdoptedProcess, "_alive",
                                lambda self: self.pid != 5020)
        result, mocks = self._run(stack)
        self.assertTrue(result)
        self.assertEqual(mocks["_wait_for_self_restarted_agent"].call_count, 2)
        self.assertEqual(gc.JVM_PID, 5021)
        # The dead candidate must not be offered again.
        self.assertIn(5020, mocks["_wait_for_self_restarted_agent"]
                      .call_args[1]["exclude"])

    def test_maintenance_guard_applies_before_a_full_relogin(self):
        # Adoption itself needs no auth, so it runs ahead of the v0.5.10
        # guard — but this branch does re-auth, inside the window where
        # IBKR's auth server is still draining.
        stack = self._adopting(port_open=False, texts={"Username"})
        stack.append(patch.object(gc, "_is_ibkr_maintenance_window",
                                  return_value=True))
        stack.append(patch.object(gc, "_apply_maintenance_recovery_delay"))
        result, mocks = self._run(stack)
        self.assertTrue(result)
        mocks["_apply_maintenance_recovery_delay"].assert_called_once()
        mocks["_redrive_login"].assert_called_once()

    def test_disclaimers_dismissed_while_waiting(self):
        opened = iter([False, True])
        stack = self._adopting(buttons={"Accept"})
        stack[3] = patch.object(gc, "is_api_port_open",
                                side_effect=lambda *a, **k: next(opened, True))
        with patch.object(gc, "SAFE_DISMISS_BUTTONS", ["Accept"]):
            result, mocks = self._run(stack)
        self.assertTrue(result)
        mocks["agent_click"].assert_called_with("Accept")


class TestRecoverAdoptsSelfRestartFirst(unittest.TestCase):
    """_recover_jvm_or_escalate ordering for issue #23: adoption runs
    before the maintenance-window guard and the fast restart, only when
    the old JVM is gone and AUTO_RESTART_ADOPT is on; every adoption
    failure falls through to the previous behaviour."""

    def setUp(self):
        self._saved = {k: getattr(gc, k) for k in
                       ("GATEWAY_PROC", "_AUTO_RESTART_ADOPT")}
        gc.GATEWAY_PROC = None
        gc._AUTO_RESTART_ADOPT = True

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(gc, k, v)

    def test_adoption_success_skips_relaunch(self):
        with patch.object(gc, "_adopt_self_restarted_gateway",
                          return_value=True) as adopt, \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=True), \
             patch.object(gc, "_apply_maintenance_recovery_delay") as delay, \
             patch.object(gc, "do_restart_in_place") as restart, \
             patch.object(gc, "_escalate_to_jvm_restart") as escalate:
            self.assertTrue(gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0))
            adopt.assert_called_once_with("JVM exited with code 0", exit_code=0)
            delay.assert_not_called()   # ordered BEFORE the 8-min guard
            restart.assert_not_called()
            escalate.assert_not_called()

    def test_adoption_failure_falls_through_to_fast_restart(self):
        with patch.object(gc, "_adopt_self_restarted_gateway",
                          return_value=False), \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=False), \
             patch.object(gc, "do_restart_in_place", return_value=True) as restart, \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown:
            self.assertTrue(gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0))
            restart.assert_called_once()
            teardown.assert_not_called()  # nothing was adopted

    def test_half_adopted_instance_is_torn_down_before_fallback(self):
        adopted = MagicMock(spec=gc._AdoptedProcess)
        adopted.pid = 5020
        adopted.poll.return_value = None

        def _adopt_but_fail(reason, *, exit_code=None):
            gc.GATEWAY_PROC = adopted
            return False

        with patch.object(gc, "_adopt_self_restarted_gateway",
                          side_effect=_adopt_but_fail), \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=False), \
             patch.object(gc, "do_restart_in_place", return_value=True) as restart, \
             patch.object(gc, "_teardown_jvm_for_restart") as teardown:
            self.assertTrue(gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0))
            teardown.assert_called_once()
            restart.assert_called_once()

    def test_adoption_exception_falls_through(self):
        with patch.object(gc, "_adopt_self_restarted_gateway",
                          side_effect=RuntimeError("boom")), \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=False), \
             patch.object(gc, "do_restart_in_place", return_value=True) as restart:
            self.assertTrue(gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0))
            restart.assert_called_once()

    def test_env_kill_switch_disables_adoption(self):
        gc._AUTO_RESTART_ADOPT = False
        with patch.object(gc, "_adopt_self_restarted_gateway") as adopt, \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=False), \
             patch.object(gc, "do_restart_in_place", return_value=True):
            self.assertTrue(gc._recover_jvm_or_escalate(
                "JVM exited with code 0", exit_code=0))
            adopt.assert_not_called()

    def test_alive_jvm_skips_adoption(self):
        # "monitor_loop re-auth failed" arrives with the JVM still up;
        # adoption only makes sense once the old JVM is gone.
        gc.GATEWAY_PROC = MagicMock()
        gc.GATEWAY_PROC.poll.return_value = None
        with patch.object(gc, "_adopt_self_restarted_gateway") as adopt, \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=False), \
             patch.object(gc, "do_restart_in_place", return_value=True):
            self.assertTrue(gc._recover_jvm_or_escalate("monitor_loop re-auth failed"))
            adopt.assert_not_called()

    def test_unobservable_exit_gets_maintenance_guard(self):
        # An adopted JVM's exit code can't be observed; treat it like 0
        # for the v0.5.10 guard (conservative). A real crash (non-zero)
        # still bypasses the guard.
        with patch.object(gc, "_adopt_self_restarted_gateway", return_value=False), \
             patch.object(gc, "_is_ibkr_maintenance_window", return_value=True), \
             patch.object(gc, "_apply_maintenance_recovery_delay") as delay, \
             patch.object(gc, "do_restart_in_place", return_value=True):
            gc._recover_jvm_or_escalate(
                "adopted Gateway JVM exited",
                exit_code=gc._EXIT_STATUS_UNOBSERVABLE)
            delay.assert_called_once()
            delay.reset_mock()
            gc._recover_jvm_or_escalate("JVM exited with code 1", exit_code=1)
            delay.assert_not_called()

    def test_env_parse_defaults_on(self):
        self.assertIs(gc._coerce_yes_no("yes") is not False, True)
        self.assertIs(gc._coerce_yes_no("no") is not False, False)
        # Unrecognised values must not silently disable the fix.
        self.assertIs(gc._coerce_yes_no("banana") is not False, True)


class TestAttemptReauthSplit(unittest.TestCase):
    """attempt_reauth keeps its contract after the _redrive_login split:
    no login dialog => True without driving anything; dialog => the
    result of _redrive_login."""

    def test_no_dialog_is_noop_true(self):
        with patch.object(gc, "agent_list", return_value=(set(), set())), \
             patch.object(gc, "_redrive_login") as redrive:
            self.assertTrue(gc.attempt_reauth(None))
            redrive.assert_not_called()

    def test_dialog_delegates_to_redrive(self):
        app = object()
        with patch.object(gc, "agent_list", return_value=({"Username"}, set())), \
             patch.object(gc, "_redrive_login", return_value=False) as redrive:
            self.assertFalse(gc.attempt_reauth(app))
            redrive.assert_called_once_with(app)


# ── PR #29: passkey prompt handling ────────────────────────────────────

_TOTP_DIALOG = (
    "OK\n=== window='Second Factor Authentication' ===\n"
    "JLabel: Enter Mobile Authenticator code\nJTextField: \n"
    "JButton: OK\nJButton: Cancel\nEND\n")
_PASSKEY_DIALOG = (
    "OK\n=== window='Second Factor Authentication' ===\n"
    "JTextArea: Use your Passkey device to complete authentication\n"
    "JButton: Authenticate >\nJButton: Cancel\nEND\n")
_RFC_SEED = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"   # RFC 6238 test vector, not a real seed


class TestPasskeyPromptPresent(unittest.TestCase):
    """Pure decision helper (CONTRIBUTING: keep the decision in a helper
    and test it directly)."""

    def test_detects_prompt(self):
        self.assertTrue(gc._passkey_prompt_present(_PASSKEY_DIALOG))

    def test_case_and_whitespace_insensitive(self):
        self.assertTrue(gc._passkey_prompt_present(
            "OK\nJTextArea: USE  YOUR\n  passkey   DEVICE now\nEND\n"))

    def test_totp_dialog_is_not_a_passkey_prompt(self):
        self.assertFalse(gc._passkey_prompt_present(_TOTP_DIALOG))

    def test_empty_and_none_are_false(self):
        self.assertFalse(gc._passkey_prompt_present(""))
        self.assertFalse(gc._passkey_prompt_present(None))


class TestHandle2faWithPasskeyHandler(unittest.TestCase):
    """Drives the real handle_2fa with a mock agent. The point is not the
    passkey feature in isolation but that adding it leaves every existing
    2FA flow untouched: TOTP still types a code and never presses
    Authenticate; the IB Key push loop still leaves the dialog alone.
    Four cases, all run on PR #29's commit before it was merged."""

    def _drive(self, dump, *, totp, gate, port=None, windows=None):
        calls = {"typed": [], "clicked": [], "consulted": 0}
        port_seq = iter(port or [False] * 200)
        win_seq = iter(windows or [[("dialog", "Second Factor Authentication", True)]] * 200)

        def window_dump(title=""):
            calls["consulted"] += 1
            return dump

        with patch.object(gc, "TOTP_SECRET", totp), \
             patch.object(gc, "_PASSKEY_AUTHENTICATE", gate), \
             patch.object(gc, "agent_windows", side_effect=lambda: next(win_seq, [])), \
             patch.object(gc, "agent_window", side_effect=window_dump), \
             patch.object(gc, "agent_list", return_value=(set(), {"Authenticate >"})), \
             patch.object(gc, "agent_labels", return_value=[
                 ("Second Factor Authentication", "Enter Mobile Authenticator code")]), \
             patch.object(gc, "agent_settext_in_window",
                          side_effect=lambda _w, t: calls["typed"].append(t) or True), \
             patch.object(gc, "agent_click_in_window",
                          side_effect=lambda _w, l: calls["clicked"].append(l) or True), \
             patch.object(gc, "agent_click", return_value=True), \
             patch.object(gc, "is_api_port_open",
                          side_effect=lambda *a, **k: next(port_seq, True)), \
             patch.object(gc, "_detect_passkey_flow", return_value=None), \
             patch.object(gc, "_reset_ccp_backoff"), \
             patch.object(gc, "time") as fake_time:
            ticks = iter(range(0, 10_000_000, 3))
            fake_time.monotonic.side_effect = lambda: next(ticks)
            fake_time.sleep = lambda *_: None
            with self.assertLogs(gc.log, level="INFO") as logs:
                result = gc.handle_2fa(object())
        calls["log"] = "\n".join(logs.output)
        calls["auth_clicks"] = [l for l in calls["clicked"] if l.startswith("Authenticate")]
        return result, calls

    def test_totp_flow_unchanged_with_gate_on(self):
        result, c = self._drive(_TOTP_DIALOG, totp=_RFC_SEED, gate=True)
        self.assertTrue(result)
        self.assertEqual(len(c["typed"]), 1)
        self.assertRegex(c["typed"][0], r"^\d{6}$")
        self.assertEqual(c["auth_clicks"], [])
        self.assertGreaterEqual(c["consulted"], 1, "handler must run, not be bypassed")

    def test_totp_flow_unchanged_with_gate_off(self):
        result, c = self._drive(_TOTP_DIALOG, totp=_RFC_SEED, gate=False)
        self.assertTrue(result)
        self.assertEqual(len(c["typed"]), 1)
        self.assertEqual(c["auth_clicks"], [])

    def test_passkey_dialog_gate_on_presses_authenticate_never_types(self):
        result, c = self._drive(_PASSKEY_DIALOG, totp=_RFC_SEED, gate=True)
        self.assertTrue(result)
        self.assertEqual(c["typed"], [], "a TOTP code must never be typed into a passkey dialog")
        self.assertEqual(c["auth_clicks"], ["Authenticate >"])

    def test_passkey_dialog_no_totp_gate_on(self):
        result, c = self._drive(_PASSKEY_DIALOG, totp="", gate=True)
        self.assertTrue(result)
        self.assertEqual(c["typed"], [])
        self.assertEqual(c["auth_clicks"], ["Authenticate >"])

    def test_passkey_dialog_gate_off_fails_loud_and_touches_nothing(self):
        # Default behaviour: same reason string as v0.8.1's browser-window
        # detection, so existing monitors keep matching; no click, no code.
        result, c = self._drive(_PASSKEY_DIALOG, totp=_RFC_SEED, gate=False)
        self.assertFalse(result)
        self.assertEqual(c["typed"], [])
        self.assertEqual(c["auth_clicks"], [])
        self.assertIn('ALERT_2FA_FAILED', c["log"])
        self.assertIn('passkey/WebAuthn 2FA flow - unattended login not supported', c["log"])
        self.assertIn('PASSKEY_AUTHENTICATE=yes', c["log"])

    def test_ib_key_push_flow_unchanged(self):
        # No TOTP secret, normal dialog: the loop the issue #23 reporter
        # and every IB Key user runs. Dialog is dismissed on the 3rd poll
        # and the port opens — success must arrive that way, untouched.
        dialog = [("dialog", "Second Factor Authentication", True)]
        result, c = self._drive(_TOTP_DIALOG, totp="", gate=True,
                                port=[False, False, True],
                                windows=[[dialog[0]], [dialog[0]]] + [[]] * 100)
        self.assertTrue(result)
        self.assertEqual(c["typed"], [])
        self.assertEqual(c["auth_clicks"], [])
        self.assertGreaterEqual(c["consulted"], 1, "handler runs in the IB Key loop too")


class TestPasskeyGateEnvParsing(unittest.TestCase):
    def test_default_off(self):
        self.assertIs(gc._coerce_yes_no("") is True, False)

    def test_yes_enables(self):
        self.assertIs(gc._coerce_yes_no("yes") is True, True)

    def test_garbage_stays_off(self):
        # An unrecognised value must not silently enable a 2FA behaviour.
        self.assertIs(gc._coerce_yes_no("banana") is True, False)


if __name__ == "__main__":
    unittest.main(verbosity=2)

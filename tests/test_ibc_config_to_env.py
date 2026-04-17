"""Unit tests for scripts/ibc_config_to_env.py — the IBC config.ini
migration tool.

Covers:
  - parse_ibc_config: strips comments, blank lines, malformed lines;
    preserves order and line numbers
  - DIRECT_MAPPINGS: key rename and value transforms
  - SPECIAL_MAPPINGS: ExitAfterSecondFactorAuthenticationTimeout →
    TWOFA_TIMEOUT_ACTION enum
  - INFORMATIONAL / UNSUPPORTED: warnings fire, no env output
  - trading_mode_hint: TWS_USERID becomes TWS_USERID_PAPER under paper
  - emit_env / emit_docker / emit_compose: output shape
"""

import io
import os
import sys
import unittest

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO, "scripts"))
import ibc_config_to_env as ibc  # noqa: E402


class TestParseIbcConfig(unittest.TestCase):
    def test_simple_pairs(self):
        text = "IbLoginId=user1\nTradingMode=paper\n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(pairs, [("IbLoginId", "user1", 1),
                                 ("TradingMode", "paper", 2)])

    def test_skips_comments_and_blanks(self):
        text = "# leading comment\n\nIbLoginId=user1\n; alt comment\nTradingMode=paper\n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(len(pairs), 2)
        self.assertEqual(pairs[0], ("IbLoginId", "user1", 3))
        self.assertEqual(pairs[1], ("TradingMode", "paper", 5))

    def test_whitespace_stripped(self):
        text = "   IbLoginId   =   user1   \n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(pairs, [("IbLoginId", "user1", 1)])

    def test_skips_malformed_no_equals(self):
        text = "this is not a config line\nIbLoginId=user1\n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(pairs, [("IbLoginId", "user1", 2)])

    def test_skips_empty_key(self):
        text = "=orphan_value\nIbLoginId=user1\n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(pairs, [("IbLoginId", "user1", 2)])

    def test_empty_value_preserved(self):
        text = "IbLoginId=\n"
        pairs = ibc.parse_ibc_config(text)
        self.assertEqual(pairs, [("IbLoginId", "", 1)])


class TestDirectMappings(unittest.TestCase):
    def test_credentials(self):
        pairs = ibc.parse_ibc_config(
            "IbLoginId=user1\nIbPassword=secret\nTradingMode=live\n")
        env, warnings = ibc.convert(pairs)
        d = dict(env)
        self.assertEqual(d["TWS_USERID"], "user1")
        self.assertEqual(d["TWS_PASSWORD"], "secret")
        self.assertEqual(d["TRADING_MODE"], "live")
        self.assertEqual(warnings, [])

    def test_yes_no_transform(self):
        pairs = ibc.parse_ibc_config(
            "ReadOnlyApi=Yes\nAllowBlindTrading=FALSE\n")
        env, _ = ibc.convert(pairs)
        d = dict(env)
        self.assertEqual(d["READ_ONLY_API"], "yes")
        self.assertEqual(d["ALLOW_BLIND_TRADING"], "no")

    def test_yes_no_unrecognized_warns(self):
        pairs = ibc.parse_ibc_config("ReadOnlyApi=maybe\n")
        env, warnings = ibc.convert(pairs)
        self.assertEqual(env, [])
        self.assertTrue(any("READ_ONLY_API" in w and "don't recognise" in w
                            for w in warnings))

    def test_existing_session_action_normalised(self):
        pairs = ibc.parse_ibc_config(
            "ExistingSessionDetectedAction=Primary\n")
        env, _ = ibc.convert(pairs)
        self.assertEqual(dict(env)["EXISTING_SESSION_DETECTED_ACTION"],
                         "primary")

    def test_command_server_port_aliases(self):
        for k in ("CommandServerPort", "IbControllerPort"):
            with self.subTest(k=k):
                pairs = ibc.parse_ibc_config(f"{k}=7462\n")
                env, _ = ibc.convert(pairs)
                self.assertEqual(dict(env)["CONTROLLER_COMMAND_SERVER_PORT"],
                                 "7462")


class TestSpecialMappings(unittest.TestCase):
    def test_exit_after_2fa_timeout_yes_becomes_exit(self):
        pairs = ibc.parse_ibc_config(
            "ExitAfterSecondFactorAuthenticationTimeout=yes\n")
        env, _ = ibc.convert(pairs)
        self.assertEqual(dict(env)["TWOFA_TIMEOUT_ACTION"], "exit")

    def test_exit_after_2fa_timeout_no_becomes_none(self):
        pairs = ibc.parse_ibc_config(
            "ExitAfterSecondFactorAuthenticationTimeout=no\n")
        env, _ = ibc.convert(pairs)
        self.assertEqual(dict(env)["TWOFA_TIMEOUT_ACTION"], "none")


class TestInformationalAndUnsupported(unittest.TestCase):
    def test_informational_twofactordevice_warns_only(self):
        pairs = ibc.parse_ibc_config("TwoFactorDevice=mobile\n")
        env, warnings = ibc.convert(pairs)
        self.assertEqual(env, [])
        self.assertTrue(any("handled implicitly" in w for w in warnings))

    def test_unsupported_fix_warns(self):
        pairs = ibc.parse_ibc_config("FIX=yes\nFIXLoginId=fixuser\n")
        env, warnings = ibc.convert(pairs)
        self.assertEqual(env, [])
        self.assertTrue(any("FIX" in w and "not supported" in w
                            for w in warnings))

    def test_unknown_key_warns(self):
        pairs = ibc.parse_ibc_config("TotallyMadeUpKey=foo\n")
        env, warnings = ibc.convert(pairs)
        self.assertEqual(env, [])
        self.assertTrue(any("unknown" in w for w in warnings))

    def test_case_insensitive_key_match(self):
        # IBC's config is case-sensitive in parser but real-world users
        # often type ibloginid lowercase. We match case-insensitively.
        pairs = ibc.parse_ibc_config("ibloginid=user1\n")
        env, _ = ibc.convert(pairs)
        self.assertEqual(dict(env).get("TWS_USERID"), "user1")


class TestTradingModeHint(unittest.TestCase):
    def test_paper_hint_renames_credentials(self):
        pairs = ibc.parse_ibc_config("IbLoginId=puser\nIbPassword=ppass\n")
        env, _ = ibc.convert(pairs, trading_mode_hint="paper")
        d = dict(env)
        self.assertIn("TWS_USERID_PAPER", d)
        self.assertIn("TWS_PASSWORD_PAPER", d)
        self.assertNotIn("TWS_USERID", d)

    def test_live_hint_leaves_credentials_alone(self):
        pairs = ibc.parse_ibc_config("IbLoginId=user\nIbPassword=pass\n")
        env, _ = ibc.convert(pairs, trading_mode_hint="live")
        d = dict(env)
        self.assertIn("TWS_USERID", d)
        self.assertNotIn("TWS_USERID_PAPER", d)

    def test_paper_hint_does_not_affect_non_credential_vars(self):
        pairs = ibc.parse_ibc_config(
            "IbLoginId=u\nCommandServerPort=7462\n")
        env, _ = ibc.convert(pairs, trading_mode_hint="paper")
        d = dict(env)
        self.assertIn("TWS_USERID_PAPER", d)
        self.assertEqual(d["CONTROLLER_COMMAND_SERVER_PORT"], "7462")


class TestEmitters(unittest.TestCase):
    def test_emit_env_format(self):
        buf = io.StringIO()
        ibc.emit_env([("TWS_USERID", "u"), ("TRADING_MODE", "paper")], buf)
        self.assertEqual(buf.getvalue(),
                         "TWS_USERID=u\nTRADING_MODE=paper\n")

    def test_emit_docker_format(self):
        buf = io.StringIO()
        ibc.emit_docker([("TWS_USERID", "u")], buf)
        # shlex.quote leaves simple alphanumeric values unquoted
        self.assertIn("-e TWS_USERID=u", buf.getvalue())

    def test_emit_docker_quotes_spaces(self):
        buf = io.StringIO()
        ibc.emit_docker([("BYPASS_WARNING", "Yes, Continue")], buf)
        # shlex.quote wraps the whole KEY=value token in single quotes
        # when the value contains whitespace.
        self.assertIn("'BYPASS_WARNING=Yes, Continue'", buf.getvalue())

    def test_emit_compose_format(self):
        buf = io.StringIO()
        ibc.emit_compose(
            [("TWS_USERID", "u"), ("TRADING_MODE", "paper")], buf)
        out = buf.getvalue()
        self.assertIn("environment:", out)
        self.assertIn('TWS_USERID: "u"', out)
        self.assertIn('TRADING_MODE: "paper"', out)

    def test_emit_compose_escapes_quotes(self):
        buf = io.StringIO()
        ibc.emit_compose([("SOME_VAR", 'he said "hi"')], buf)
        self.assertIn('\\"hi\\"', buf.getvalue())


class TestEndToEnd(unittest.TestCase):
    """Run main() with a temp config file and capture stdout/stderr."""

    def test_main_env_format(self):
        import tempfile
        cfg = ("# sample IBC config\n"
               "IbLoginId=user1\n"
               "IbPassword=secret\n"
               "TradingMode=paper\n"
               "ReadOnlyApi=yes\n"
               "FIX=yes\n"  # should warn
               )
        with tempfile.NamedTemporaryFile("w", suffix=".ini", delete=False) as f:
            f.write(cfg)
            path = f.name
        try:
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout, sys.stderr = io.StringIO(), io.StringIO()
            try:
                rc = ibc.main([path, "--format", "env", "--quiet"])
            finally:
                out = sys.stdout.getvalue()
                err = sys.stderr.getvalue()
                sys.stdout, sys.stderr = old_stdout, old_stderr
        finally:
            os.unlink(path)

        self.assertEqual(rc, 0)
        self.assertIn("TWS_USERID=user1", out)
        self.assertIn("TWS_PASSWORD=secret", out)
        self.assertIn("TRADING_MODE=paper", out)
        self.assertIn("READ_ONLY_API=yes", out)
        self.assertIn("FIX", err)  # warning for unsupported key


if __name__ == "__main__":
    unittest.main(verbosity=2)

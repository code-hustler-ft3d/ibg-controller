#!/usr/bin/env python3
"""Convert an IBC config.ini to equivalent ibg-controller env vars.

Usage:
  ibc_config_to_env.py [--format env|docker|compose] [--trading-mode paper|live]
                       [path/to/config.ini]

If no path is given, reads from stdin. Conversion output goes to stdout,
warnings (unsupported keys, ambiguous conversions) go to stderr.

Formats:
  env      KEY=value lines (docker --env-file compatible, the default)
  docker   `-e KEY='value'` flags (space-separated, for `docker run`)
  compose  YAML under an `environment:` block (for docker-compose.yml)

Goal: let an IBC user sanity-check a migration without rewriting 50
lines of config by hand. The tool is advisory — always review the
output before deploying, especially for BYPASS_WARNING (IBC's binary
knob becomes a list of exact button labels) and for credentials
(prefer Docker secrets / *_FILE env vars over pasting passwords into
a .env file that gets committed).
"""

import argparse
import shlex
import sys
from typing import Callable, Dict, List, Optional, Tuple


# ── IBC key → ibg-controller env var mapping ──────────────────────────
#
# Four categories:
#   DIRECT       — straight 1:1 rename, value passes through
#   TRANSFORM    — rename plus a value transform (e.g. yes/no normalisation)
#   INFORMATIONAL — IBC key whose behaviour the controller already does
#                   implicitly; no env var needed, we emit a stderr note
#   UNSUPPORTED  — IBC key with no controller equivalent; emit a warning

def _identity(v: str) -> Optional[str]:
    return v


def _yes_no(v: str) -> Optional[str]:
    vl = v.strip().lower()
    if vl in ("yes", "true", "1", "on"):
        return "yes"
    if vl in ("no", "false", "0", "off"):
        return "no"
    return None


def _existing_session_action(v: str) -> Optional[str]:
    vl = v.strip().lower()
    if vl in ("primary", "primaryoverride", "secondary", "manual"):
        return vl
    return None


# IBC → (controller env var, transform). Keys are IBC's exact case;
# matching on the parser side is case-insensitive.
DIRECT_MAPPINGS: Dict[str, Tuple[str, Callable[[str], Optional[str]]]] = {
    "IbLoginId": ("TWS_USERID", _identity),
    "IbPassword": ("TWS_PASSWORD", _identity),
    "TradingMode": ("TRADING_MODE", lambda v: v.strip().lower()),
    "ReadOnlyApi": ("READ_ONLY_API", _yes_no),
    "ExistingSessionDetectedAction": (
        "EXISTING_SESSION_DETECTED_ACTION", _existing_session_action),
    "AutoRestartTime": ("AUTO_RESTART_TIME", _identity),
    "AllowBlindTrading": ("ALLOW_BLIND_TRADING", _yes_no),
    "CommandServerPort": ("CONTROLLER_COMMAND_SERVER_PORT", _identity),
    "IbControllerPort": ("CONTROLLER_COMMAND_SERVER_PORT", _identity),
    "BindAddress": ("CONTROLLER_COMMAND_SERVER_HOST", _identity),
    "TwsSettingsPath": ("TWS_SETTINGS_PATH", _identity),
    "SecondFactorAuthenticationExitInterval": (
        "TWOFA_EXIT_INTERVAL", _identity),
    "ReloginAfterSecondFactorAuthenticationTimeout": (
        "RELOGIN_AFTER_TWOFA_TIMEOUT", _yes_no),
    "SaveTwsSettingsAt": ("SAVE_TWS_SETTINGS", _identity),
    "TimeZone": ("TIME_ZONE", _identity),
}


# IBC keys that map to a different env-var shape with caveats. The
# callable returns a list of (env_name, env_value) pairs or None to skip.
def _exit_after_2fa_timeout(v: str) -> Optional[List[Tuple[str, str]]]:
    yn = _yes_no(v)
    if yn is None:
        return None
    # IBC's flag is boolean. Our env var is an enum: "exit" | "restart" |
    # "none". yes → exit; no → none.
    return [("TWOFA_TIMEOUT_ACTION", "exit" if yn == "yes" else "none")]


SPECIAL_MAPPINGS: Dict[str, Callable[[str], Optional[List[Tuple[str, str]]]]] = {
    "ExitAfterSecondFactorAuthenticationTimeout": _exit_after_2fa_timeout,
}


# IBC keys that have no env equivalent but the controller already
# implements the behaviour (so the user doesn't need to do anything).
INFORMATIONAL: Dict[str, str] = {
    "TwoFactorDevice":
        "controller polls for the 2FA dialog to be dismissed (same "
        "approach IBC takes); no env var needed",
    "SecondFactorDevice":
        "alias for TwoFactorDevice — handled implicitly",
    "LogToConsole":
        "controller always logs to stdout/stderr; no env var needed",
}


# IBC keys with no controller equivalent.
UNSUPPORTED: Dict[str, str] = {
    "FIX":
        "FIX CTCI mode isn't supported — stay on IBC for FIX flows",
    "FIXLoginId":
        "FIX CTCI mode isn't supported",
    "FIXPassword":
        "FIX CTCI mode isn't supported",
    "CustomConfig":
        "the controller reads env vars directly; there is no IBC-style "
        "config.ini for it to consume at runtime",
    "MinimizeMainWindow":
        "no-op in the headless Docker target",
    "MaximizeMainWindow":
        "no-op in the headless Docker target",
    "StoreSettingsOnServer":
        "the controller doesn't override Gateway's own default here",
    "SuppressInfoMessages":
        "controller logging is controlled by CONTROLLER_DEBUG only",
    "LogComponents":
        "controller logging is controlled by CONTROLLER_DEBUG only; "
        "no per-component log filter",
    "IbAutoClosedown":
        "not a direct map — set AUTO_LOGOFF_TIME=HH:MM if you want the "
        "controller to drive Gateway's Auto Log Off Time field",
    "ClosedownAt":
        "IBC-specific scheduled-shutdown — use Gateway's own "
        "AUTO_LOGOFF_TIME/AUTO_RESTART_TIME via the equivalent env vars",
    "AcceptNonBrokerageAccountWarning":
        "if this is 'yes' in IBC, add the exact button text to "
        "BYPASS_WARNING (e.g. BYPASS_WARNING=\"I Accept\") — IBC's "
        "boolean maps to the controller's explicit allowlist",
    "BypassWarning":
        "IBC's yes/no becomes the controller's allowlist: "
        "BYPASS_WARNING=\"Yes,Continue,Acknowledge\" (comma-separated "
        "exact button labels). Review the labels that appear in your "
        "own Gateway dialogs and list them here",
    "ControlFrom":
        "the controller uses an auth token instead "
        "(CONTROLLER_COMMAND_SERVER_AUTH_TOKEN=<secret>); see "
        "README.md Security section",
    "SendTWSLogsToConsole":
        "no equivalent — Gateway's own logs go to launcher.log",
    "IbDir":
        "IBC-installation-dir-only; the controller's path is set by "
        "TWS_PATH (Gateway install dir) which you probably don't need "
        "to set in a standard Docker deployment",
}


def parse_ibc_config(text: str) -> List[Tuple[str, str, int]]:
    """Parse IBC config.ini format: KEY=VALUE lines, `#` or `;` comments,
    blank lines allowed. Returns a list of (key, value, line_number) so
    warnings can cite source lines. Case is preserved on keys."""
    result = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line[0] in "#;":
            continue
        if "=" not in line:
            # malformed — skip with no warning; IBC itself tolerates these
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        result.append((key, value, i))
    return result


def convert(
    pairs: List[Tuple[str, str, int]],
    trading_mode_hint: Optional[str] = None,
) -> Tuple[List[Tuple[str, str]], List[str]]:
    """Apply the mapping tables. Returns (env_mappings, warnings).

    trading_mode_hint: if set, rename credential keys so they target the
    requested mode slot. E.g. ``IbLoginId`` → ``TWS_USERID_PAPER`` when
    the hint is 'paper'. This matters because ibg-controller's dual-mode
    uses per-mode credential env vars, while IBC runs one process per
    mode with the same keys for each."""
    # Case-insensitive lookup table
    direct_ci = {k.lower(): (k, v) for k, v in DIRECT_MAPPINGS.items()}
    special_ci = {k.lower(): (k, v) for k, v in SPECIAL_MAPPINGS.items()}
    informational_ci = {k.lower(): (k, v) for k, v in INFORMATIONAL.items()}
    unsupported_ci = {k.lower(): (k, v) for k, v in UNSUPPORTED.items()}

    env: List[Tuple[str, str]] = []
    warnings: List[str] = []

    for key, value, lineno in pairs:
        kl = key.lower()

        if kl in direct_ci:
            canonical_key, (env_name, transform) = direct_ci[kl]
            transformed = transform(value)
            if transformed is None:
                warnings.append(
                    f"line {lineno}: {key}={value!r} has a value we don't "
                    f"recognise; skipping (target env var: {env_name})")
                continue
            if trading_mode_hint == "paper" and env_name in (
                    "TWS_USERID", "TWS_PASSWORD"):
                env_name = env_name + "_PAPER"
            env.append((env_name, transformed))
            continue

        if kl in special_ci:
            canonical_key, transform = special_ci[kl]
            result = transform(value)
            if result is None:
                warnings.append(
                    f"line {lineno}: {key}={value!r} not recognised; "
                    "skipping")
                continue
            env.extend(result)
            continue

        if kl in informational_ci:
            canonical_key, note = informational_ci[kl]
            warnings.append(f"line {lineno}: {key} is handled implicitly — {note}")
            continue

        if kl in unsupported_ci:
            canonical_key, note = unsupported_ci[kl]
            warnings.append(f"line {lineno}: {key} is not supported — {note}")
            continue

        warnings.append(
            f"line {lineno}: {key} is unknown — not in the IBC config "
            "dictionary this tool knows about; please review manually")

    return env, warnings


def emit_env(mappings: List[Tuple[str, str]], out) -> None:
    for name, value in mappings:
        # No shell quoting here — .env files are read line-by-line by
        # docker without shell expansion. Values with newlines are not
        # supported by the .env format at all; we don't try to handle
        # them.
        out.write(f"{name}={value}\n")


def emit_docker(mappings: List[Tuple[str, str]], out) -> None:
    # shlex.quote does POSIX-safe single-quoting; good enough for
    # docker run shell consumption.
    flags = [f"-e {shlex.quote(f'{n}={v}')}" for n, v in mappings]
    out.write(" ".join(flags))
    out.write("\n")


def emit_compose(mappings: List[Tuple[str, str]], out) -> None:
    out.write("    environment:\n")
    for name, value in mappings:
        # Quote all values with double quotes; escape embedded quotes.
        # This is YAML-safe for the narrow set of values IBC produces.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        out.write(f'      {name}: "{escaped}"\n')


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert an IBC config.ini to ibg-controller env vars.")
    parser.add_argument("path", nargs="?", default="-",
                        help="Path to IBC config.ini (or '-' for stdin; "
                             "default: stdin)")
    parser.add_argument("--format", choices=("env", "docker", "compose"),
                        default="env",
                        help="Output format (default: env)")
    parser.add_argument("--trading-mode", choices=("live", "paper"),
                        default=None,
                        help="If set, renames TWS_USERID/TWS_PASSWORD to "
                             "the per-mode variants (TWS_USERID_PAPER, "
                             "TWS_PASSWORD_PAPER) for dual-mode setups")
    parser.add_argument("--quiet", action="store_true",
                        help="Suppress the informational banner on stderr")
    args = parser.parse_args(argv)

    if args.path == "-":
        text = sys.stdin.read()
        source = "<stdin>"
    else:
        try:
            with open(args.path, encoding="utf-8") as f:
                text = f.read()
            source = args.path
        except OSError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2

    pairs = parse_ibc_config(text)
    env, warnings = convert(pairs, trading_mode_hint=args.trading_mode)

    # Print warnings to stderr with a leading banner. Always print them
    # even with --quiet because a missing warning could silently mean
    # the migration was wrong.
    if not args.quiet:
        print(f"# ibc_config_to_env.py: read {len(pairs)} key=value pair(s) "
              f"from {source}, produced {len(env)} env mapping(s), "
              f"{len(warnings)} warning(s)", file=sys.stderr)
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)

    if args.format == "env":
        emit_env(env, sys.stdout)
    elif args.format == "docker":
        emit_docker(env, sys.stdout)
    elif args.format == "compose":
        emit_compose(env, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())

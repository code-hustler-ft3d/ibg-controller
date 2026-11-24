"""Unit tests for .github/scripts/gateway_bump.py: the installer download
and the pin rewrite the daily Gateway workflow proposes."""

import io
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".github", "scripts"))
import gateway_bump as bump  # noqa: E402

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
X64, ARM = "a" * 64, "b" * 64
DOCKERFILE = (f"ARG UBUNTU_IMAGE=ubuntu:26.04@{OLD}\n"
              "ARG IB_GATEWAY_VERSION=10.50.1g\n"
              f"ARG IB_GATEWAY_SHA256_X64={'c' * 64}\n"
              f"ARG IB_GATEWAY_SHA256_ARM={'d' * 64}\n"
              "FROM ${UBUNTU_IMAGE} AS setup\nARG IB_GATEWAY_VERSION\n")
README = "| IB Gateway 10.x | Release images pin **10.50.1g** (IBKR's `stable` channel) |\n"
INSTALLER = b"#!/bin/sh\n\n# INSTALL4J_JAVA_HOME_OVERRIDE=\n" + b"x" * 5000


class FakeResponse(io.BytesIO):
    def __init__(self, body, url="https://download2.interactivebrokers.com/x.sh"):
        super().__init__(body)
        self._url = url
        self.headers = {}

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class TestApply(unittest.TestCase):

    def test_moves_the_pins_in_the_dockerfile_and_readme(self):
        d, r = bump.apply(DOCKERFILE, README, "10.50.1h", X64, ARM, NEW)
        self.assertIn("ARG IB_GATEWAY_VERSION=10.50.1h\n", d)
        self.assertIn(f"ARG IB_GATEWAY_SHA256_X64={X64}\n", d)
        self.assertIn(f"ARG IB_GATEWAY_SHA256_ARM={ARM}\n", d)
        self.assertIn(f"ARG UBUNTU_IMAGE=ubuntu:26.04@{NEW}\n", d)
        for gone in (OLD, "c" * 64, "d" * 64, "10.50.1g"):
            self.assertNotIn(gone, d)
        # The stage's own bare re-declaration is left alone.
        self.assertIn("AS setup\nARG IB_GATEWAY_VERSION\n", d)
        self.assertIn("pin **10.50.1h**", r)

    def test_without_an_ubuntu_digest_the_base_stays(self):
        d, _ = bump.apply(DOCKERFILE, README, "10.50.1h", X64, ARM)
        self.assertIn(f"ARG UBUNTU_IMAGE=ubuntu:26.04@{OLD}\n", d)

    def test_refuses_anything_but_a_version_checksums_and_a_digest(self):
        for version, x64, arm, ubuntu in (
                ("10.50.1h; rm -rf /", X64, ARM, NEW), ("latest", X64, ARM, NEW),
                ("10.50.1h", "abc", ARM, NEW), ("10.50.1h", X64, ARM + "\nARG X=1", NEW),
                ("10.50.1h", X64.upper(), ARM, NEW), ("10.50.1h", X64, ARM, "sha256:abc"),
                ("10.50.1h", X64, ARM, "")):
            with self.assertRaises(SystemExit, msg=(version, x64, arm, ubuntu)):
                bump.apply(DOCKERFILE, README, version, x64, arm, ubuntu)

    def test_refuses_a_dockerfile_without_the_pins(self):
        for broken in (DOCKERFILE.replace(f"@{OLD}", ""),
                       DOCKERFILE.replace("ARG IB_GATEWAY_VERSION=10.50.1g\n", ""),
                       DOCKERFILE.replace("ARG IB_GATEWAY_SHA256_ARM=", "ARG ARM=")):
            with self.assertRaises(SystemExit):
                bump.apply(broken, README, "10.50.1h", X64, ARM, NEW)

    def test_reads_the_current_pin(self):
        self.assertEqual(bump.current_pin(DOCKERFILE), ("10.50.1g", "ubuntu:26.04", OLD))


class TestIbkrFeed(unittest.TestCase):
    """The feed's values go into file names, a branch name and an issue."""

    def _feed(self, body):
        with patch.object(bump, "_get", return_value=(body, {})):
            return bump.ibkr_build("stable")

    def test_reads_version_and_build(self):
        self.assertEqual(
            self._feed(b'ibgatewaystable_callback({"buildVersion":"10.50.1h",'
                       b'"buildDateTime":"20261116 16:02:08"});'),
            ("10.50.1h", "20261116 16:02:08"))

    def test_a_version_must_look_like_one(self):
        for body in (b'{"buildVersion":"10.50.1h` @everyone | x"}', b'{"buildVersion":"../../etc"}',
                     b'{"buildVersion":""}', b"<html>maintenance</html>"):
            with self.assertRaises(SystemExit, msg=body):
                self._feed(body)

    def test_an_odd_build_stamp_is_dropped_not_passed_on(self):
        self.assertEqual(self._feed(b'{"buildVersion":"10.50.1h","buildDateTime":"<b>now</b>"}'),
                         ("10.50.1h", ""))

    def test_only_the_two_channels(self):
        with self.assertRaises(SystemExit):
            bump.ibkr_build("stable/../x")


class TestDownload(unittest.TestCase):
    """Nothing is stored unless the file checks out."""

    def _download(self, body, url=None, lo=1000, hi=100_000):
        resp = FakeResponse(body, url) if url else FakeResponse(body)
        with tempfile.TemporaryDirectory() as d, \
                patch.object(bump, "_open", return_value=resp), \
                patch.object(bump, "MIN_BYTES", lo), patch.object(bump, "MAX_BYTES", hi):
            return bump.download("stable", "x64", os.path.join(d, "f.sh"))

    def test_hashes_what_it_wrote(self):
        import hashlib
        self.assertEqual(self._download(INSTALLER),
                         (hashlib.sha256(INSTALLER).hexdigest(), len(INSTALLER)))

    def test_asks_ibkrs_host_for_the_channels_file(self):
        with tempfile.TemporaryDirectory() as d, \
                patch.object(bump, "_open", return_value=FakeResponse(INSTALLER)) as op, \
                patch.object(bump, "MIN_BYTES", 1000):
            bump.download("latest", "arm", os.path.join(d, "f.sh"))
        self.assertEqual(op.call_args[0][0],
                         "https://download2.interactivebrokers.com/installers/ibgateway/"
                         "latest-standalone/ibgateway-latest-standalone-linux-arm.sh")

    def test_a_redirect_to_another_host_is_refused(self):
        for url in ("https://example.com/x.sh", "http://download2.interactivebrokers.com/x.sh",
                    "https://download2.interactivebrokers.com.example.com/x.sh"):
            with self.assertRaises(SystemExit, msg=url):
                self._download(INSTALLER, url)

    def test_a_small_file_is_refused(self):
        with self.assertRaises(SystemExit):
            self._download(b"#!/bin/sh\n# INSTALL4J\n")

    def test_an_endless_file_is_refused(self):
        with self.assertRaises(SystemExit):
            self._download(INSTALLER, hi=2000)

    def test_something_that_is_not_an_installer_is_refused(self):
        for body in (b"<html>" + b"x" * 5000, b"#!/bin/sh\n" + b"x" * 5000,
                     b"\n#!/bin/sh\n# INSTALL4J" + b"x" * 5000):
            with self.assertRaises(SystemExit, msg=body[:20]):
                self._download(body)


class TestFetch(unittest.TestCase):

    FILES = {"x64": (X64, 360_312_436), "arm": (ARM, 221_123_393)}

    def _fetch(self, feed, version="10.50.1h", theirs=None, ubuntu=NEW):
        feed = iter(feed)
        out = io.StringIO()
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Dockerfile"), "w") as f:
                f.write(DOCKERFILE)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with patch.object(bump, "ibkr_build", side_effect=lambda c: next(feed)), \
                     patch.object(bump, "download", side_effect=lambda c, a, p: self.FILES[a]), \
                     patch.object(bump, "witness",
                                  side_effect=lambda c, v, a: (theirs or {}).get(a)), \
                     patch.object(bump, "ubuntu_digest", return_value=ubuntu), \
                     patch.object(sys, "argv", ["gateway_bump.py", "fetch", "--version", version,
                                                "--dir", d, "--body", "b.md"]), \
                     redirect_stdout(out):
                    bump.main()
                with open("b.md") as f:
                    self.body = f.read()
            finally:
                os.chdir(cwd)
        return dict(line.split("=", 1) for line in out.getvalue().split())

    SAME = [("10.50.1h", "20261116 16:02:08")] * 2

    def test_prints_the_checksums_and_writes_the_report(self):
        got = self._fetch(self.SAME, theirs={"x64": X64, "arm": ARM})
        self.assertEqual(got, {"x64": X64, "arm": ARM, "ubuntu": NEW, "short": X64[:12]})
        self.assertIn("moved from `10.50.1g` to `10.50.1h` (build 20261116 16:02:08)", self.body)
        self.assertIn(f"| x64 installer | `{X64}` (360,312,436 bytes) |", self.body)
        self.assertIn("| gnzsnz's archive of the same files | same checksums |", self.body)
        self.assertIn(f"| Ubuntu base | `ubuntu:26.04@{NEW}`, **moved** from `{OLD}`", self.body)

    def test_a_feed_that_names_another_version_stops_it(self):
        with self.assertRaises(SystemExit):
            self._fetch([("10.50.1i", "20261201 10:00:00")] * 2)

    def test_a_release_landing_during_the_download_stops_it(self):
        for after in (("10.50.1i", "20261201 10:00:00"), ("10.50.1h", "20261117 09:00:00")):
            with self.assertRaises(SystemExit, msg=after):
                self._fetch([self.SAME[0], after])

    def test_a_malformed_version_is_refused_before_anything_is_fetched(self):
        with self.assertRaises(SystemExit):
            self._fetch(self.SAME, version="10.50.1h/../../x")

    def test_a_different_witness_is_reported_not_fatal(self):
        self._fetch(self.SAME, theirs={"x64": "e" * 64, "arm": ARM})
        self.assertIn(f"**differs**: x64 `{'e' * 64}`, arm `{ARM}`", self.body)

    def test_no_witness_reads_as_none_published(self):
        self._fetch(self.SAME)
        self.assertIn("| gnzsnz's archive of the same files | nothing published for this version |",
                      self.body)

    def test_an_unreadable_ubuntu_tag_keeps_the_pin(self):
        got = self._fetch(self.SAME, ubuntu=None)
        self.assertEqual(got["ubuntu"], OLD)
        self.assertIn(f"could not be read; the pin stays at `ubuntu:26.04@{OLD}`", self.body)

    def test_an_unchanged_ubuntu_digest_says_so(self):
        self._fetch(self.SAME, ubuntu=OLD)
        self.assertIn(f"| Ubuntu base | `ubuntu:26.04@{OLD}` (unchanged) |", self.body)


class TestCheck(unittest.TestCase):

    def _check(self, version, channel=None):
        out = io.StringIO()
        argv = ["gateway_bump.py", "check"] + (["--channel", channel] if channel else [])
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Dockerfile"), "w") as f:
                f.write(DOCKERFILE)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with patch.object(bump, "ibkr_build", return_value=(version, "20261116 16:02:08")), \
                     patch.object(sys, "argv", argv), redirect_stdout(out):
                    bump.main()
            finally:
                os.chdir(cwd)
        return out.getvalue().splitlines()

    def test_the_pinned_version_proposes_nothing(self):
        self.assertEqual(self._check("10.50.1g")[0], "changed=false")

    def test_another_stable_version_is_a_change(self):
        self.assertEqual(self._check("10.50.1h"),
                         ["changed=true", "version=10.50.1h", "build=20261116 16:02:08"])

    def test_latest_is_always_archived_on_request(self):
        self.assertEqual(self._check("10.50.1g", "latest")[0], "changed=true")


class TestWitness(unittest.TestCase):

    def test_reads_a_published_checksum(self):
        with patch.object(bump, "_get", return_value=((X64 + "  ibgateway-x.sh\n").encode(), {})) as g:
            self.assertEqual(bump.witness("stable", "10.50.1h", "x64"), X64)
        self.assertEqual(g.call_args[0][0],
                         "https://github.com/gnzsnz/ib-gateway-docker/releases/download/"
                         "ibgateway-stable%4010.50.1h/ibgateway-10.50.1h-standalone-linux-x64.sh.sha256")

    def test_anything_else_reads_as_none(self):
        for body in (b"Not Found", b"", b"<script>alert(1)</script> x", X64.upper().encode()):
            with patch.object(bump, "_get", return_value=(body, {})):
                self.assertIsNone(bump.witness("stable", "10.50.1h", "x64"), body)
        with patch.object(bump, "_get", side_effect=urllib.error.URLError("down")):
            self.assertIsNone(bump.witness("stable", "10.50.1h", "x64"))


class TestUbuntuDigest(unittest.TestCase):

    def _digest(self, arches, digest=NEW):
        import json
        index = {"manifests": [{"platform": {"os": "linux", "architecture": a}} for a in arches]}
        replies = iter([(json.dumps({"token": "t"}).encode(), {}),
                        (json.dumps(index).encode(), {"Docker-Content-Digest": digest})])
        with patch.object(bump, "_get", side_effect=lambda *a, **k: next(replies)):
            return bump.ubuntu_digest("ubuntu:26.04")

    def test_reads_the_index_digest(self):
        self.assertEqual(self._digest(["amd64", "arm64", "s390x"]), NEW)

    def test_both_architectures_must_be_there(self):
        self.assertIsNone(self._digest(["amd64"]))

    def test_a_malformed_digest_reads_as_unknown(self):
        self.assertIsNone(self._digest(["amd64", "arm64"], digest="sha256:abc` @x"))

    def test_an_unreachable_registry_reads_as_unknown(self):
        with patch.object(bump, "_get", side_effect=urllib.error.URLError("down")):
            self.assertIsNone(bump.ubuntu_digest("ubuntu:26.04"))


class TestBumpWorkflow(unittest.TestCase):
    """The issue is what tells the owner; it must not be lost. And a stored
    installer must never be replaced."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", ".github", "workflows", "gateway-bump.yml")) as f:
            self.wf = f.read()

    def test_the_issue_check_comes_before_the_download_and_the_branch_check(self):
        # A branch left without its issue gets the issue (and CI) next run.
        self.assertLess(self.wf.index("creator=github-actions%5Bbot%5D"),
                        self.wf.index("gateway_bump.py fetch"))
        self.assertLess(self.wf.index("gateway_bump.py fetch"), self.wf.index("git ls-remote"))
        recovery = self.wf[self.wf.index("exists without its issue"):self.wf.index("else\n            python3")]
        self.assertIn('gh workflow run ci.yml --ref "$branch"', recovery)

    def test_the_issue_check_does_not_use_search(self):
        # Search lags behind new issues; two runs close together would
        # both propose the same version.
        self.assertNotIn("--search", self.wf)
        self.assertNotIn("gh issue list", self.wf)

    def test_the_issue_title_is_what_the_check_looks_for(self):
        self.assertIn('endswith(\\" is ${VERSION}\\")', self.wf)
        self.assertIn('--title "Gateway: IBKR stable is ${VERSION}"', self.wf)

    def test_a_failed_assignment_keeps_the_issue(self):
        create = self.wf.index("gh issue create")
        self.assertNotIn("--assignee", self.wf[create:create + 200])
        self.assertIn('--add-assignee "$GITHUB_REPOSITORY_OWNER" \\\n            || echo "::warning::', self.wf)

    def test_the_installers_are_stored_before_ci_is_started(self):
        # CI's build reads them from the release.
        self.assertLess(self.wf.index('gh release upload "$ARCHIVE"'),
                        self.wf.index("gh workflow run ci.yml"))

    def test_a_stored_installer_is_never_replaced(self):
        self.assertNotIn("--clobber", self.wf)
        self.assertNotIn("gh release delete", self.wf)
        self.assertIn('elif [ "$have" = "sha256:${want}" ]; then', self.wf)

    def test_latest_stores_but_proposes_nothing(self):
        step = self.wf[self.wf.index("- name: Push the bump branch"):]
        self.assertIn("(inputs.channel || 'stable') == 'stable'", step[:400])

    def test_the_channel_is_a_fixed_choice(self):
        self.assertIn("type: choice\n        options: [stable, latest]", self.wf)
        # Never interpolated into a script: it reaches the shell as a variable.
        for line in self.wf.splitlines():
            if "${{ inputs.channel" in line:
                self.assertRegex(line, r"^\s+(CHANNEL: |if: )", line)


class TestRepoFilesMatchTheScript(unittest.TestCase):
    """The real Dockerfile and README must keep the shapes the workflow
    rewrites, or the daily job fails; and ci.yml must keep the `pinned`
    entries that test a bump, since the bump can't edit workflow files."""

    def setUp(self):
        root = os.path.join(os.path.dirname(__file__), "..")

        def read(path):
            with open(os.path.join(root, path), encoding="utf-8") as f:
                return f.read()
        self.read = read

    def test_real_files_apply_cleanly(self):
        nd, nr = bump.apply(self.read("Dockerfile"), self.read("README.md"), "99.99.9z", X64, ARM, NEW)
        self.assertIn("ARG IB_GATEWAY_VERSION=99.99.9z\n", nd)
        self.assertIn(f"ARG IB_GATEWAY_SHA256_X64={X64}\n", nd)
        self.assertIn(f"ARG IB_GATEWAY_SHA256_ARM={ARM}\n", nd)
        self.assertIn(f"@{NEW}\n", nd)
        self.assertIn("pin **99.99.9z**", nr)

    def test_every_pin_is_declared_once_and_imported_by_the_stages(self):
        d = self.read("Dockerfile")
        for name in ("UBUNTU_IMAGE", "IB_GATEWAY_VERSION", "IB_GATEWAY_SHA256_X64",
                     "IB_GATEWAY_SHA256_ARM", "IBC_SHA256", "IBC_CONFIG_TMPL_SHA256"):
            self.assertEqual(len([l for l in d.splitlines() if l.startswith(f"ARG {name}=")]), 1, name)
        first_from = d.index("\nFROM ")
        self.assertLess(d.index("ARG IB_GATEWAY_SHA256_ARM="), first_from)
        # Both stages build on the one pinned Ubuntu.
        self.assertEqual([l for l in d.splitlines() if l.startswith("FROM ")],
                         ["FROM ${UBUNTU_IMAGE} AS setup", "FROM ${UBUNTU_IMAGE}"])

    def test_every_download_is_checked_before_it_is_used(self):
        d = self.read("Dockerfile")
        run = d[d.index("RUN apt-get update"):d.index("# The image.")]
        self.assertEqual(run.count("curl -fsSL"), 3)
        checks = ['verify "${installer}" "${sum}"', 'verify ibc.zip "${IBC_SHA256}"',
                  'verify config.ini.tmpl "${IBC_CONFIG_TMPL_SHA256}"']
        uses = ['"./${installer}" -q', "unzip -q ibc.zip", "cp config.ini.tmpl"]
        for check, use in zip(checks, uses):
            self.assertLess(run.rindex("curl -fsSL"), run.index(check), check)
            self.assertLess(run.index(check), run.index(use), use)
        # The comparison itself, not `sha256sum --check`, which skips a
        # line it can't parse: an empty pin would go unchecked.
        self.assertNotIn("--check", run)
        self.assertIn('[ "${have}" != "$2" ]', run)

    def test_the_checksum_comparison_refuses_an_empty_or_wrong_pin(self):
        # The same shell the Dockerfile runs, against a real file.
        import hashlib
        import subprocess
        d = self.read("Dockerfile")
        start = d.index(" && verify() {") + 4
        fn = d[start:d.index(' && verify "${installer}"')].replace("\\\n", "\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "f")
            with open(path, "wb") as f:
                f.write(b"installer bytes")
            good = hashlib.sha256(b"installer bytes").hexdigest()
            for pin, want in ((good, 0), ("", 1), ("0" * 64, 1), (good.upper(), 1),
                              (good[:-1], 1), (good + " ", 1)):
                r = subprocess.run(["sh", "-c", f'{fn}\nverify "$1" "$2"', "sh", path, pin],
                                   capture_output=True, text=True)
                self.assertEqual(r.returncode, want, (pin, r.stderr))

    def test_ci_builds_the_pins_on_both_architectures(self):
        ci = self.read(".github/workflows/ci.yml")
        self.assertIn("          - gateway: pinned\n            runner: ubuntu-24.04\n", ci)
        self.assertIn("          - gateway: pinned\n            runner: ubuntu-24.04-arm\n", ci)

    def test_no_apostrophe_ends_a_quoted_container_script_early(self):
        # The image checks are shell scripts passed as one single-quoted
        # argument (`bash -c '...'`). An apostrophe in a comment inside one
        # ends the argument there, and the rest runs on the runner instead.
        ci = self.read(".github/workflows/ci.yml")
        blocks = [b for b in ci.split("\n      - name: ") if " -c '" in b]
        self.assertGreaterEqual(len(blocks), 4)
        for block in blocks:
            # The script only: the step's name and YAML comments may say "IBC's".
            script = block.split("        run: |\n", 1)[1]
            self.assertEqual(script.count("'") % 2, 0, block.splitlines()[0])

    def test_the_build_context_holds_what_the_dockerfile_copies(self):
        d, ignore = self.read("Dockerfile"), self.read(".dockerignore")
        copied = set()
        for line in d.splitlines():
            if line.startswith("COPY ") and "--from=" not in line:
                copied.update(line.split()[1:-1])
        self.assertTrue(copied)
        for path in copied:
            self.assertIn(f"!{path}\n", ignore, path)
            if not path.startswith("dist/"):
                self.assertTrue(os.path.exists(os.path.join(os.path.dirname(__file__), "..", path)), path)


if __name__ == "__main__":
    unittest.main()

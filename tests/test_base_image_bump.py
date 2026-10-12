"""Unit tests for .github/scripts/base_image_bump.py: the pin rewrite the
daily base-image workflow proposes."""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".github", "scripts"))
import base_image_bump as bump  # noqa: E402

OLD = "sha256:" + "1" * 64
NEW = "sha256:" + "2" * 64
DOCKERFILE = (f"ARG UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:10.50.1f@{OLD}\n"
              "FROM ${UPSTREAM_IMAGE}\nARG UPSTREAM_IMAGE\nARG IB_GATEWAY_VERSION=10.50.1f\n")
README = "| IB Gateway 10.x | Release images pin **10.50.1f** (gnzsnz `:stable` line) |\n"


class TestApply(unittest.TestCase):

    def test_moves_the_pin_in_the_dockerfile_and_readme(self):
        d, r = bump.apply(DOCKERFILE, README, "10.50.1g", NEW)
        self.assertIn(f"ib-gateway:10.50.1g@{NEW}", d)
        self.assertIn("ARG IB_GATEWAY_VERSION=10.50.1g", d)
        self.assertNotIn(OLD, d)
        self.assertIn("pin **10.50.1g**", r)

    def test_refuses_anything_but_a_version_and_a_digest(self):
        for version, digest in (("10.50.1g; rm -rf /", NEW), ("10.50.1g", "sha256:abc"),
                                ("latest", NEW), ("10.50.1g", NEW + "\nARG X=1")):
            with self.assertRaises(SystemExit):
                bump.apply(DOCKERFILE, README, version, digest)

    def test_refuses_a_dockerfile_without_a_digest_pin(self):
        with self.assertRaises(SystemExit):
            bump.apply("ARG UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:stable\n", README, "10.50.1g", NEW)

    def test_reads_the_current_pin(self):
        self.assertEqual(bump.current_pin(DOCKERFILE), ("10.50.1f", OLD))


class TestIssueText(unittest.TestCase):
    """A commit message from upstream goes into a public issue."""

    def test_mentions_links_html_and_pipes_are_neutralised(self):
        out = bump.safe_text("Update @everyone <img src=x> [ok](https://evil) | x")
        for bad in ("@", "<", ">", "[", "]", "|", "https://evil)"):
            self.assertNotIn(bad, out)

    def test_the_report_shows_the_commit_as_inline_code(self):
        stable = {"version": "10.50.1g", "digest": NEW, "arches": ["amd64", "arm64"],
                  "source": bump.SOURCE, "revision": "a" * 40, "onbuild": []}
        body = bump.report("10.50.1f", stable, "10.50.1g", "Merge www.evil.com #397")
        self.assertIn("`Merge www.evil.com #397`", body)

    def test_ibkrs_version_must_look_like_one(self):
        from unittest.mock import patch
        for body, want in ((b'{"buildVersion":"10.50.1f"}', "10.50.1f"),
                           (b'{"buildVersion":"10.50.1f` @everyone | x"}', None)):
            with patch.object(bump, "_get", return_value=(body, {})):
                self.assertEqual(bump.ibkr_stable_version(), want)

    def test_length_is_capped(self):
        self.assertEqual(len(bump.safe_text("a" * 500)), 100)


class TestCheckRefuses(unittest.TestCase):
    """The daily check proposes nothing unless the image checks out."""

    STABLE = {"version": "10.50.1g", "digest": NEW, "arches": ["amd64", "arm64"],
              "source": bump.SOURCE, "revision": "a" * 40, "onbuild": []}

    def _check(self, stable, commit="Update stable to 10.50.1g (#456)"):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "Dockerfile"), "w") as f:
                f.write(DOCKERFILE)
            cwd = os.getcwd()
            os.chdir(d)
            try:
                with patch.object(bump, "registry_stable", return_value=stable), \
                     patch.object(bump, "gnzsnz_commit", return_value=commit), \
                     patch.object(bump, "ibkr_stable_version", return_value="10.50.1g"), \
                     patch.object(sys, "argv", ["base_image_bump.py", "check", "--body", "b.md"]):
                    bump.main()
            finally:
                os.chdir(cwd)

    def test_onbuild_instructions_are_refused(self):
        with self.assertRaises(SystemExit):
            self._check({**self.STABLE, "onbuild": ["arm64"]})

    def test_a_commit_off_the_default_branch_is_refused(self):
        with self.assertRaises(SystemExit):
            self._check(self.STABLE, commit=None)

    def test_a_foreign_source_label_is_refused(self):
        with self.assertRaises(SystemExit):
            self._check({**self.STABLE, "source": "https://github.com/someone/else"})

    def test_an_unchanged_digest_proposes_nothing(self):
        import io
        from contextlib import redirect_stdout
        out = io.StringIO()
        with redirect_stdout(out):
            self._check({**self.STABLE, "digest": OLD})
        self.assertEqual(out.getvalue().strip(), "changed=false")


class TestBumpWorkflowOrder(unittest.TestCase):
    """The issue is what tells the owner; it must not be lost."""

    def setUp(self):
        here = os.path.dirname(os.path.abspath(__file__))
        with open(os.path.join(here, "..", ".github", "workflows", "base-image-bump.yml")) as f:
            self.wf = f.read()

    def test_the_issue_check_comes_before_the_branch_check(self):
        # A branch left without its issue gets the issue (and CI) next run.
        self.assertLess(self.wf.index("creator=github-actions%5Bbot%5D"),
                        self.wf.index("git ls-remote"))
        recovery = self.wf[self.wf.index("exists without its issue"):self.wf.index("else\n")]
        self.assertIn('gh workflow run ci.yml --ref "$branch"', recovery)

    def test_the_issue_check_does_not_use_search(self):
        # Search lags behind new issues; two runs close together would
        # both propose the same digest.
        self.assertNotIn("--search", self.wf)
        self.assertNotIn("gh issue list", self.wf)

    def test_a_failed_assignment_keeps_the_issue(self):
        create = self.wf.index("gh issue create")
        self.assertNotIn("--assignee", self.wf[create:create + 200])
        self.assertIn('--add-assignee "$GITHUB_REPOSITORY_OWNER" \\\n            || echo "::warning::', self.wf)


class TestRepoFilesMatchTheScript(unittest.TestCase):
    """The real Dockerfile and README must keep the shapes the workflow
    rewrites, or the daily job fails; and ci.yml must keep the `pinned`
    entry that tests a bump, since the bump can't edit workflow files."""

    def test_real_files_apply_cleanly(self):
        root = os.path.join(os.path.dirname(__file__), "..")
        read = lambda p: open(os.path.join(root, p), encoding="utf-8").read()
        nd, nr = bump.apply(read("Dockerfile"), read("README.md"), "99.99.9z", NEW)
        self.assertIn("ib-gateway:99.99.9z@", nd)
        self.assertIn("pin **99.99.9z**", nr)
        self.assertIn("          - pinned\n", read(".github/workflows/ci.yml"))


if __name__ == "__main__":
    unittest.main()

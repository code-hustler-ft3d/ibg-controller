#!/usr/bin/env python3
"""Keep the release image's base on gnzsnz's `stable` line.

Used by .github/workflows/base-image-bump.yml. Standard library only.

  base_image_bump.py check --body FILE
      Compare gnzsnz/ib-gateway:stable with the Dockerfile's pinned base.
      Prints changed=, version= and digest= lines for $GITHUB_OUTPUT and
      writes a Markdown report to FILE. Exits non-zero if stable moved but
      its provenance doesn't check out, so nothing is proposed.
  base_image_bump.py apply --version V --digest D
      Rewrite the pin in the Dockerfile and README. Never a workflow file:
      GitHub refuses GITHUB_TOKEN pushes that change one, and CI's `pinned`
      matrix entry already builds whatever the Dockerfile pins.

Checked before anything is proposed: the image's OCI source label is
gnzsnz/ib-gateway-docker; its revision label is a commit on that repo's
default branch (a fork's commit is also served under the parent's URL, so
"exists" isn't enough); it has amd64 and arm64; and no architecture
carries ONBUILD instructions, which would run during our own build.
These labels are set by whoever pushed the image and gnzsnz publishes no
signatures, so they are sanity checks, not proof. IBKR's own stable
channel is compared and reported, not required.
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

UPSTREAM = "gnzsnz/ib-gateway"
SOURCE = "https://github.com/gnzsnz/ib-gateway-docker"
IBKR_STABLE = ("https://download2.interactivebrokers.com/installers/ibgateway/"
               "stable-standalone/version.json")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+[a-z]?$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PIN_RE = re.compile(r"^ARG UPSTREAM_IMAGE=ghcr\.io/gnzsnz/ib-gateway:(\S+)@(sha256:[0-9a-f]{64})$", re.M)
IDX = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"
MAN = "application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json"


def current_pin(dockerfile):
    m = PIN_RE.search(dockerfile)
    if not m:
        raise SystemExit("Dockerfile: no digest-pinned ARG UPSTREAM_IMAGE line")
    return m.group(1), m.group(2)


def _get(url, accept=None, token=None):
    headers = {"User-Agent": "ibg-controller-base-image-bump"}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
        return r.read(), r.headers


def registry_stable():
    """Version, index digest, OCI labels and architectures of :stable."""
    tok = json.loads(_get(f"https://ghcr.io/token?scope=repository:{UPSTREAM}:pull")[0])["token"]
    body, headers = _get(f"https://ghcr.io/v2/{UPSTREAM}/manifests/stable", IDX, tok)
    digest = headers.get("Docker-Content-Digest", "")
    index = json.loads(body)
    arches = {m["platform"]["architecture"]: m["digest"] for m in index.get("manifests", [])
              if m.get("platform", {}).get("os") == "linux"}
    if "amd64" not in arches:
        raise SystemExit("gnzsnz stable has no amd64 image")
    configs = {}
    for arch, child in arches.items():
        manifest = json.loads(_get(f"https://ghcr.io/v2/{UPSTREAM}/manifests/{child}", MAN, tok)[0])
        configs[arch] = json.loads(_get(f"https://ghcr.io/v2/{UPSTREAM}/blobs/{manifest['config']['digest']}",
                                        "*/*", tok)[0])["config"]
    config = configs["amd64"]
    env = dict(e.split("=", 1) for e in config.get("Env", []) if "=" in e)
    labels = config.get("Labels") or {}
    return {"version": env.get("IB_GATEWAY_VERSION", ""), "digest": digest,
            "source": labels.get("org.opencontainers.image.source", ""),
            "revision": labels.get("org.opencontainers.image.revision", ""),
            "arches": sorted(arches),
            "onbuild": sorted(a for a, c in configs.items() if c.get("OnBuild"))}


def ibkr_stable_version():
    try:
        text = _get(IBKR_STABLE)[0].decode("utf-8", "replace")
    except (urllib.error.URLError, OSError):
        return None
    m = re.search(r'"buildVersion"\s*:\s*"([^"]+)"', text)
    # Goes into the issue body: accept only something shaped like a version.
    return m.group(1) if m and VERSION_RE.fullmatch(m.group(1)) else None


def _gh(path=""):
    url = "https://api.github.com/repos/gnzsnz/ib-gateway-docker" + (f"/{path}" if path else "")
    return json.loads(_get(url, "application/vnd.github+json", os.environ.get("GH_TOKEN"))[0])


def safe_text(text, limit=100):
    """Plain characters only, for an issue body: no @mentions, links, HTML,
    backticks or table pipes from a commit message someone else wrote. The
    report also shows it as inline code, which GitHub never turns into a
    link or an issue reference."""
    text = re.sub(r"\w+://\S*", "(link removed)", text or "")
    text = re.sub(r"[^A-Za-z0-9 ._:#()'/+-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def gnzsnz_commit(revision):
    """The commit's first line if it is on gnzsnz/ib-gateway-docker's
    default branch, else None."""
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        return None
    try:
        default = _gh()["default_branch"]
        status = _gh(f"compare/{default}...{revision}")["status"]
        if status not in ("identical", "behind"):
            return None
        message = _gh(f"commits/{revision}")["commit"]["message"]
    except (urllib.error.URLError, OSError, KeyError, ValueError):
        return None
    return safe_text(message.splitlines()[0] if message else "")


def apply(dockerfile, readme, version, digest):
    """Return the Dockerfile and README with the pin moved to ``version@digest``."""
    if not VERSION_RE.fullmatch(version) or not DIGEST_RE.fullmatch(digest):
        raise SystemExit(f"refusing malformed version/digest: {version!r} {digest!r}")
    current_pin(dockerfile)  # refuses a Dockerfile without a digest pin
    dockerfile = PIN_RE.sub(f"ARG UPSTREAM_IMAGE=ghcr.io/gnzsnz/ib-gateway:{version}@{digest}",
                            dockerfile, count=1)
    dockerfile, n = re.subn(r"^ARG IB_GATEWAY_VERSION=\S+$", f"ARG IB_GATEWAY_VERSION={version}",
                            dockerfile, count=1, flags=re.M)
    if n != 1:
        raise SystemExit("Dockerfile: no ARG IB_GATEWAY_VERSION line")
    readme, n = re.subn(r"(Release images pin \*\*)[^*]+(\*\*)", rf"\g<1>{version}\g<2>", readme, count=1)
    if n != 1:
        raise SystemExit("README: no 'Release images pin **...**' line")
    return dockerfile, readme


def report(pin_version, stable, ibkr, commit):
    ibkr_line = ("matches" if ibkr == stable["version"]
                 else f"**differs**: IBKR stable is `{ibkr}`" if ibkr else "could not be read")
    return f"""gnzsnz's `stable` image moved from `{pin_version}` to `{stable['version']}`.

| Check | Result |
|---|---|
| Index digest | `{stable['digest']}` |
| Architectures | {', '.join(stable['arches'])} |
| Built from | {stable['source']} @ `{stable['revision'][:12]}` (on its default branch): `{commit}` |
| ONBUILD instructions | none |
| IBKR's own stable channel | {ibkr_line} |

These come from the image's own labels, which gnzsnz doesn't sign, so they are sanity checks rather than proof.

The bump branch changes the Dockerfile pin and the README; CI was started on it, and its `pinned` entry builds this exact digest. Close this issue to reject this digest: it won't be proposed again.

**Before releasing**, check a real login on the new version: login, TOTP, post-login settings and dialogs. Nothing is merged or released automatically.
"""


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--body", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--version", required=True)
    a.add_argument("--digest", required=True)
    args = ap.parse_args()
    with open("Dockerfile", encoding="utf-8") as f:
        dockerfile = f.read()
    if args.cmd == "check":
        pin_version, pin_digest = current_pin(dockerfile)
        stable = registry_stable()
        if stable["digest"] == pin_digest:
            print("changed=false")
            return 0
        if not VERSION_RE.fullmatch(stable["version"]) or not DIGEST_RE.fullmatch(stable["digest"]):
            raise SystemExit(f"stable's version or digest is malformed: {stable!r}")
        if stable["source"] != SOURCE:
            raise SystemExit(f"stable's source label is {stable['source']!r}, not {SOURCE}")
        if stable["arches"] != ["amd64", "arm64"]:
            raise SystemExit(f"stable's architectures are {stable['arches']}")
        if stable["onbuild"]:
            raise SystemExit(f"stable carries ONBUILD instructions on {stable['onbuild']}; "
                             "they would run during our build")
        commit = gnzsnz_commit(stable["revision"])
        if commit is None:
            raise SystemExit(f"stable's revision {stable['revision']!r} isn't a commit on "
                             "gnzsnz/ib-gateway-docker's default branch")
        with open(args.body, "w", encoding="utf-8") as f:
            f.write(report(pin_version, stable, ibkr_stable_version(), commit))
        print("changed=true")
        print(f"version={stable['version']}")
        print(f"digest={stable['digest']}")
        print(f"short={stable['digest'][7:19]}")
        return 0
    with open("README.md", encoding="utf-8") as f:
        readme = f.read()
    dockerfile, readme = apply(dockerfile, readme, args.version, args.digest)
    for path, text in (("Dockerfile", dockerfile), ("README.md", readme)):
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Keep the release image on IBKR's `stable` Gateway.

Used by .github/workflows/gateway-bump.yml. Standard library only.

  gateway_bump.py check [--channel stable|latest]
      Read IBKR's version feed for the channel and compare it with the
      Dockerfile's pin. Prints changed=, version= and build= lines for
      $GITHUB_OUTPUT. `latest` is never the pin, so it always reads as
      changed: it is archived on request, not proposed.
  gateway_bump.py fetch --version V --dir DIR --body FILE [--channel ...]
      Download IBKR's installer for both architectures into DIR, named as
      the Dockerfile expects, and write a Markdown report to FILE. Prints
      x64=, arm= (SHA-256), ubuntu= and short= lines. Exits non-zero, so
      nothing is stored or proposed, if a download doesn't check out.
  gateway_bump.py apply --version V --x64 SHA --arm SHA [--ubuntu DIGEST]
      Rewrite the pins in the Dockerfile and README. Never a workflow file:
      GitHub refuses GITHUB_TOKEN pushes that change one, and CI's `pinned`
      matrix entry already builds whatever the Dockerfile pins.

Checked before anything is stored: each file comes from IBKR's download
host over HTTPS, with no redirect to another host; it is an install4j
shell installer of a plausible size; and the version feed names the same
version and build before and after the downloads, so a release landing
halfway through can't mix two builds. IBKR publishes no checksums or
signatures for these files, so that is all there is to check against.
gnzsnz/ib-gateway-docker downloads the same files independently and
publishes their SHA-256; whether they agree is reported, not required.

The Ubuntu base is pinned by digest. A proposal moves it to the tag's
current digest and says so, since our packages install from it.
"""

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

IBKR_HOST = "download2.interactivebrokers.com"
IBKR_DIR = f"https://{IBKR_HOST}/installers/ibgateway/{{channel}}-standalone"
WITNESS = ("https://github.com/gnzsnz/ib-gateway-docker/releases/download/"
           "ibgateway-{channel}%40{version}/{name}.sha256")
CHANNELS = ("stable", "latest")
ARCHES = ("x64", "arm")
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+[a-z]?$")
BUILD_RE = re.compile(r"^\d{8} \d{2}:\d{2}:\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
UBUNTU_RE = re.compile(r"^ARG UBUNTU_IMAGE=(ubuntu:\d{2}\.\d{2})@(sha256:[0-9a-f]{64})$", re.M)
PIN_RE = re.compile(r"^ARG IB_GATEWAY_VERSION=(\S+)$", re.M)
# An installer is a few hundred MB. Outside this range it is not one.
MIN_BYTES, MAX_BYTES = 50 * 1024 * 1024, 1024 * 1024 * 1024
IDX = "application/vnd.oci.image.index.v1+json, application/vnd.docker.distribution.manifest.list.v2+json"


def installer_name(version, arch):
    return f"ibgateway-{version}-standalone-linux-{arch}.sh"


def current_pin(dockerfile):
    """The pinned Gateway version, and the Ubuntu tag and digest."""
    v, u = PIN_RE.search(dockerfile), UBUNTU_RE.search(dockerfile)
    if not v or not VERSION_RE.fullmatch(v.group(1)):
        raise SystemExit("Dockerfile: no ARG IB_GATEWAY_VERSION=<version> line")
    if not u:
        raise SystemExit("Dockerfile: no digest-pinned ARG UBUNTU_IMAGE line")
    return v.group(1), u.group(1), u.group(2)


def _open(url, accept=None, token=None):
    headers = {"User-Agent": "ibg-controller-gateway-bump"}
    if accept:
        headers["Accept"] = accept
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60)


def _get(url, accept=None, token=None, limit=4 * 1024 * 1024):
    with _open(url, accept, token) as r:
        return r.read(limit), r.headers


def ibkr_build(channel):
    """(version, build date-time) from IBKR's feed for the channel. Both go
    into file names, a branch name and an issue: only the expected shapes
    are accepted."""
    if channel not in CHANNELS:
        raise SystemExit(f"unknown channel {channel!r}")
    text = _get(IBKR_DIR.format(channel=channel) + "/version.json")[0].decode("utf-8", "replace")
    v = re.search(r'"buildVersion"\s*:\s*"([^"]*)"', text)
    b = re.search(r'"buildDateTime"\s*:\s*"([^"]*)"', text)
    if not v or not VERSION_RE.fullmatch(v.group(1)):
        raise SystemExit(f"IBKR's {channel} feed has no usable buildVersion")
    build = b.group(1) if b and BUILD_RE.fullmatch(b.group(1)) else ""
    return v.group(1), build


def download(channel, arch, path):
    """Stream IBKR's installer to ``path``. Returns (sha256, size)."""
    url = f"{IBKR_DIR.format(channel=channel)}/ibgateway-{channel}-standalone-linux-{arch}.sh"
    digest, size = hashlib.sha256(), 0
    with _open(url) as r, open(path, "wb") as out:
        final = urllib.parse.urlsplit(r.geturl())
        if final.scheme != "https" or final.hostname != IBKR_HOST:
            raise SystemExit(f"{arch}: the download was redirected to {final.hostname!r}")
        head = b""
        while True:
            block = r.read(1024 * 1024)
            if not block:
                break
            if len(head) < 4096:
                head += block[:4096 - len(head)]
            size += len(block)
            if size > MAX_BYTES:
                raise SystemExit(f"{arch}: more than {MAX_BYTES} bytes; not an installer")
            digest.update(block)
            out.write(block)
    if size < MIN_BYTES:
        raise SystemExit(f"{arch}: only {size} bytes; not an installer")
    if not head.startswith(b"#!/bin/sh\n") or b"INSTALL4J" not in head:
        raise SystemExit(f"{arch}: the file is not an install4j shell installer")
    return digest.hexdigest(), size


def witness(channel, version, arch):
    """The SHA-256 gnzsnz/ib-gateway-docker publishes for the same file, or
    None if there is none to read."""
    url = WITNESS.format(channel=channel, version=version, name=installer_name(version, arch))
    try:
        text = _get(url, limit=4096)[0].decode("ascii", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None
    first = text.split()[0] if text.split() else ""
    return first if SHA_RE.fullmatch(first) else None


def witness_line(ours, theirs):
    """ours and theirs map each architecture to a SHA-256 (theirs: or None)."""
    if all(theirs.get(a) is None for a in ARCHES):
        return "nothing published for this version"
    if all(theirs.get(a) == ours[a] for a in ARCHES):
        return "same checksums"
    return "**differs**: " + ", ".join(
        f"{a} `{theirs[a]}`" if theirs.get(a) else f"{a} not published" for a in ARCHES)


def ubuntu_digest(tag):
    """The current index digest of an ``ubuntu:NN.NN`` tag with amd64 and
    arm64 images, or None if it can't be read."""
    try:
        repo, ref = "library/ubuntu", tag.split(":", 1)[1]
        tok = json.loads(_get("https://auth.docker.io/token?service=registry.docker.io"
                              f"&scope=repository:{repo}:pull")[0])["token"]
        body, headers = _get(f"https://registry-1.docker.io/v2/{repo}/manifests/{ref}", IDX, tok)
        arches = {m.get("platform", {}).get("architecture") for m in json.loads(body).get("manifests", [])
                  if m.get("platform", {}).get("os") == "linux"}
        digest = headers.get("Docker-Content-Digest", "")
    except (urllib.error.URLError, OSError, KeyError, ValueError, IndexError, TypeError):
        return None
    return digest if DIGEST_RE.fullmatch(digest) and {"amd64", "arm64"} <= arches else None


def ubuntu_line(tag, now, was):
    if now and now != was:
        return (f"`{tag}@{now}`, **moved** from `{was}`: our packages install "
                "from the new image")
    if now:
        return f"`{tag}@{now}` (unchanged)"
    return f"could not be read; the pin stays at `{tag}@{was}`"


def apply(dockerfile, readme, version, x64, arm, ubuntu=None):
    """Return the Dockerfile and README with the pins moved."""
    if not VERSION_RE.fullmatch(version) or not SHA_RE.fullmatch(x64) or not SHA_RE.fullmatch(arm):
        raise SystemExit(f"refusing malformed version/checksums: {version!r} {x64!r} {arm!r}")
    if ubuntu is not None and not DIGEST_RE.fullmatch(ubuntu):
        raise SystemExit(f"refusing malformed Ubuntu digest: {ubuntu!r}")
    _, tag, was = current_pin(dockerfile)
    for name, value in (("IB_GATEWAY_VERSION", version), ("IB_GATEWAY_SHA256_X64", x64),
                        ("IB_GATEWAY_SHA256_ARM", arm)):
        dockerfile, n = re.subn(rf"^ARG {name}=\S+$", f"ARG {name}={value}", dockerfile,
                                count=1, flags=re.M)
        if n != 1:
            raise SystemExit(f"Dockerfile: no ARG {name}=<value> line")
    if ubuntu:
        dockerfile = UBUNTU_RE.sub(f"ARG UBUNTU_IMAGE={tag}@{ubuntu}", dockerfile, count=1)
    readme, n = re.subn(r"(Release images pin \*\*)[^*]+(\*\*)", rf"\g<1>{version}\g<2>", readme, count=1)
    if n != 1:
        raise SystemExit("README: no 'Release images pin **...**' line")
    return dockerfile, readme


def report(channel, pin_version, version, build, files, theirs, ubuntu_row):
    """files maps each architecture to (sha256, size)."""
    rows = "\n".join(f"| {a} installer | `{files[a][0]}` ({files[a][1]:,} bytes) |" for a in ARCHES)
    built = f" (build {build})" if build else ""
    return f"""IBKR's `{channel}` Gateway moved from `{pin_version}` to `{version}`{built}.

| Check | Result |
|---|---|
{rows}
| Downloaded from | `{IBKR_HOST}` over HTTPS; its version feed named the same build before and after |
| gnzsnz's archive of the same files | {witness_line({a: files[a][0] for a in ARCHES}, theirs)} |
| Ubuntu base | {ubuntu_row} |

IBKR publishes no checksums or signatures for its installers, so these are sanity checks rather than proof.

Both installers are stored under the `build-inputs` release. The bump branch changes the Dockerfile's pins and the README; CI was started on it, and its `pinned` entry builds these exact files. Close this issue to reject this version: it won't be proposed again.

**Before releasing**, check a real login on the new version: login, TOTP, post-login settings and dialogs. Nothing is merged or released automatically.
"""


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--channel", choices=CHANNELS, default="stable")
    f = sub.add_parser("fetch")
    f.add_argument("--channel", choices=CHANNELS, default="stable")
    f.add_argument("--version", required=True)
    f.add_argument("--dir", required=True)
    f.add_argument("--body", required=True)
    a = sub.add_parser("apply")
    a.add_argument("--version", required=True)
    a.add_argument("--x64", required=True)
    a.add_argument("--arm", required=True)
    a.add_argument("--ubuntu")
    args = ap.parse_args()
    with open("Dockerfile", encoding="utf-8") as fh:
        dockerfile = fh.read()
    pin_version, tag, pin_ubuntu = current_pin(dockerfile)

    if args.cmd == "check":
        version, build = ibkr_build(args.channel)
        changed = args.channel != "stable" or version != pin_version
        print(f"changed={'true' if changed else 'false'}")
        print(f"version={version}")
        print(f"build={build}")
        return 0

    if args.cmd == "fetch":
        if not VERSION_RE.fullmatch(args.version):
            raise SystemExit(f"refusing malformed version: {args.version!r}")
        before = ibkr_build(args.channel)
        if before[0] != args.version:
            raise SystemExit(f"IBKR's {args.channel} feed says {before[0]}, not {args.version}")
        files = {arch: download(args.channel, arch,
                                os.path.join(args.dir, installer_name(args.version, arch)))
                 for arch in ARCHES}
        if ibkr_build(args.channel) != before:
            raise SystemExit("IBKR's feed changed during the download; the files may not be one build")
        theirs = {arch: witness(args.channel, args.version, arch) for arch in ARCHES}
        now = ubuntu_digest(tag)
        with open(args.body, "w", encoding="utf-8") as fh:
            fh.write(report(args.channel, pin_version, args.version, before[1], files, theirs,
                            ubuntu_line(tag, now, pin_ubuntu)))
        print(f"x64={files['x64'][0]}")
        print(f"arm={files['arm'][0]}")
        print(f"ubuntu={now or pin_ubuntu}")
        print(f"short={files['x64'][0][:12]}")
        return 0

    with open("README.md", encoding="utf-8") as fh:
        readme = fh.read()
    dockerfile, readme = apply(dockerfile, readme, args.version, args.x64, args.arm, args.ubuntu)
    for path, text in (("Dockerfile", dockerfile), ("README.md", readme)):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())

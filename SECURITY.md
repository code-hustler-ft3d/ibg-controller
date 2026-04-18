# Security & supply chain

This doc covers how `ibg-controller`'s release artifacts are produced,
signed, and verified, plus the threat model and how to report a
vulnerability.

## Published artifacts per release

Every tag push produces three artifacts, all reproducible from the
same git ref:

1. **Source tarball** — `ibg-controller-<version>.tar.gz`, attached
   to the [GitHub release](https://github.com/code-hustler-ft3d/ibg-controller/releases).
   Contains `gateway_controller.py`, the agent jar, `install.sh`,
   docs, and the `ibc_config_to_env.py` migration tool.
2. **Container image** — `ghcr.io/code-hustler-ft3d/ibg-controller:<version>`,
   multi-tagged with `<version>`, `<major>.<minor>`, and `latest`.
3. **SBOM** — `sbom.spdx.json` attached to the GitHub release AND
   attached as a signed cosign attestation to the container image.
   Generated from the running image layers with
   [syft](https://github.com/anchore/syft).

## Supply chain controls

### Image signing (keyless)

All container images are signed with [cosign](https://github.com/sigstore/cosign)
using Sigstore's keyless flow. The signing identity is the GitHub
Actions OIDC token for this repository's `.github/workflows/release-image.yml`
workflow, which means:

- **No private key is involved** — there's no "key file got leaked"
  failure mode.
- **Identity is tied to a specific workflow file in a specific repo.**
  A forked repo that reuses our workflow will produce a different
  identity (their workflow path, their repo URL), so verification
  will not accept their signatures as ours.
- **Rotating keys is not a concept here** — each release is signed
  by the then-current workflow. Revoking a compromised release means
  publishing a new release with the fix.

### SBOM provenance

The SBOM is generated *after* the image is pushed and signed, against
the pushed image by digest. Then the SBOM is attached to the image
via `cosign attest`, so consumers can cryptographically verify that
the SBOM corresponds to the specific image digest they're pulling.

## How to verify what you pulled

Install cosign:
```bash
# macOS
brew install cosign

# Linux
wget -O cosign 'https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-amd64'
chmod +x cosign && sudo mv cosign /usr/local/bin/
```

Verify the image signature:
```bash
IMAGE=ghcr.io/code-hustler-ft3d/ibg-controller:v0.5.6   # replace with your tag

cosign verify \
  --certificate-identity-regexp '^https://github.com/code-hustler-ft3d/ibg-controller/\.github/workflows/release-image\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE"
```

Expected output includes `Verified OK` and a JSON block showing the
OIDC identity (GitHub repo + workflow file + git ref) that signed it.
If `certificate-identity-regexp` *doesn't* match
`code-hustler-ft3d/ibg-controller`, the image wasn't signed by this
project's workflow — treat it as untrusted.

Verify + extract the SBOM:
```bash
cosign verify-attestation --type spdxjson \
  --certificate-identity-regexp '^https://github.com/code-hustler-ft3d/ibg-controller/\.github/workflows/release-image\.yml@' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$IMAGE" \
  | jq -r '.payload' | base64 -d | jq '.predicate' > sbom.spdx.json

# Inspect dependencies
jq -r '.packages[] | "\(.name) \(.versionInfo)"' sbom.spdx.json | sort -u
```

## Pinning for reproducibility

`:latest` moves with every tag. For reproducible deployments, pin to a
digest:

```bash
# Resolve the current digest for the tag you want
docker buildx imagetools inspect ghcr.io/code-hustler-ft3d/ibg-controller:v0.5.6 \
  | awk '/^Digest:/ {print $2}'

# Use the digest in your compose / k8s / docker run
image: ghcr.io/code-hustler-ft3d/ibg-controller@sha256:abc123...
```

The digest is printed in the CI log of every release (see the
"Print verification recipe" step in the `Release image` workflow run),
and is also the ref `cosign` reports after a successful verify.

## Threat model & what's NOT in scope

`ibg-controller` drives a GUI login into IBKR's Gateway. That auth
flow has some inherent constraints:

- **`TWS_USERID` / `TWS_PASSWORD` must be readable by the controller
  process.** Use Docker secrets / `*_FILE` env-var pattern / your
  orchestrator's secret store — don't paste them into a committed
  `.env`.
- **`TOTP_SECRET` (if used) is a bearer secret.** Anyone who can read
  it can generate your 2FA codes. Same storage guidance as passwords.
- **The controller itself doesn't ship with remote-management.** The
  optional `CONTROLLER_COMMAND_SERVER_*` is opt-in, token-authed, and
  intended to be bound to loopback / private networks.

What we don't protect against:

- A compromised host running the controller. If an attacker has root
  on the box, they have the container process memory, which contains
  your credentials.
- A compromised IBKR account via other means (phishing, credential
  reuse, SIM-swap on your 2FA phone). The controller just logs in
  with the creds you give it.
- Supply chain compromise of `gnzsnz/ib-gateway` upstream base image.
  We pin versions in `docs/FROM_IBC.md` §Compatibility and verify
  against them in CI, but we depend on upstream doing its own
  auditing.

## Reporting a vulnerability

**Don't open a public GitHub issue for a security bug.**

Use GitHub's private vulnerability reporting flow for this repo
(Settings → Security → Report a vulnerability), or email the address
listed on the maintainer's GitHub profile. Expect an initial response
within 5 business days.

Good reports include:

1. Affected version(s).
2. Minimal reproduction steps.
3. Impact assessment (what a successful attacker gains).
4. Any suggested fix direction (optional but appreciated).

We'll coordinate a private fix + release, then publish an advisory
with credit to the reporter (unless the reporter prefers anonymity).

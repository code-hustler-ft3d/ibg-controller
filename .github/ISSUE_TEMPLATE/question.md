---
name: Question
about: Ask about behaviour, configuration, or how to do X with ibg-controller
title: ""
labels: question
assignees: ""
---

**What are you trying to do?**
One or two sentences. Include the use case, not just the technical
question — it often shapes the answer.

**What have you tried?**
Commands run, env vars set, docs you've already read. Links to
specific doc sections are fine:
- [`README.md`](../blob/main/README.md)
- [`docs/FROM_IBC.md`](../blob/main/docs/FROM_IBC.md) — if migrating from IBC
- [`docs/OBSERVABILITY.md`](../blob/main/docs/OBSERVABILITY.md) — for health/alert questions
- [`docs/UPGRADING.md`](../blob/main/docs/UPGRADING.md) — for version-to-version migrations
- [`SECURITY.md`](../blob/main/SECURITY.md) — for cosign/SBOM/pinning questions

**What happened?**
Logs, error output, unexpected behaviour. Redact credentials and
account numbers before pasting.

**Environment**
- Controller version (from `curl /health | jq .version`):
- Gateway version (`TWS_MAJOR_VRSN`):
- Architecture: amd64 / arm64
- Trading mode: live / paper / both
- Image source: pulled `ghcr.io/code-hustler-ft3d/ibg-controller:...` / built locally

If this might be a bug rather than a question, switch to the
[Bug report](?template=bug_report.md) template instead — it asks
for more diagnostics and routes into the triage label.

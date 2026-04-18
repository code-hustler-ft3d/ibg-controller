## What this PR does

<!-- One-sentence summary. If replacing/extending an existing feature,
link to the prior PR or CHANGELOG entry. -->

## Why

<!-- The user-facing problem this solves, or the non-obvious motivation
behind a refactor. Skip if the "What" makes the "Why" self-evident. -->

## Checklist

- [ ] `make clean && make && make test` passes (166+ tests green)
- [ ] No credentials, account numbers, or PII in the diff
- [ ] CHANGELOG.md updated under the next version's heading
- [ ] `docs/OBSERVABILITY.md` updated (if touching ALERT tokens, `/health`, or env vars listed there)
- [ ] `docs/FROM_IBC.md` updated (if changing anything that maps to an IBC key)
- [ ] `docs/UPGRADING.md` gets a new `### vX.Y.Z` section if the change is operator-visible
- [ ] Commit messages imperative ("fix X", not "fixed X" or "WIP")

See [`CONTRIBUTING.md`](../blob/main/CONTRIBUTING.md) for the
"Adding a new..." walkthroughs covering the most common extension
points (ALERT tokens, dialog handlers, env vars, IBC-key mappings).

---
name: Feature request
about: Propose a new capability or ask about an IBC parity gap
title: ""
labels: enhancement
assignees: ""
---

**Is this an IBC feature you need?**
If yes, name the IBC `config.ini` key(s) you'd be replacing. Check
[`docs/FROM_IBC.md` §Unsupported IBC keys](../blob/main/docs/FROM_IBC.md#unsupported-ibc-keys)
first — your key may already be intentionally unsupported, in which
case you'll find the rationale and any workaround there.

**What problem does this solve?**
Describe the scenario where the current controller falls short.
Concrete > abstract — "our paper account hits CCP lockout every
Monday at 04:00 UTC" > "we need better lockout handling".

**What would you like to see?**
Sketch the proposed interface: new env var, new ALERT token, new
command-server verb, new CLI flag, etc. Don't worry about
implementation — the maintainer will bikeshed naming in review.

**Non-goals / alternatives considered**
What would make this proposal wrong or premature? Any workarounds
that already exist?

**Are you open to submitting a PR?**
Not required, but useful to know. If yes, see
[`CONTRIBUTING.md`](../blob/main/CONTRIBUTING.md) — the
"Adding a new..." sections have step-by-step walkthroughs for the
most common extension points.

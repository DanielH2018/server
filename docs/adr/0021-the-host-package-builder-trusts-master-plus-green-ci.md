---
id: "0021"
title: The host-package builder trusts master plus green CI, and checks no signature
status: Accepted
date: 2026-10-14
governs:
  - "hosts/lib/host_package_builder.py#the anchor is a commit on origin/master's first-parent chain"
---

# ADR-0021: The host-package builder trusts master plus green CI, and checks no signature

## Status

Accepted. The operator chose option (a) of
[H3](../target-architecture/host/h3-pi-pilot.md) *Open questions* 1 on 2026-10-14, with the
answer "(a) Master + green CI."

## Context

Slice H3 adds a LAN builder on `daniel-box`. The builder renders each host's package from
the repository, signs the apt repository, and publishes it. Every host's updater then
installs those packages as root. So the commit the builder accepts is the root of trust
for every host.

Contract C5 in [the contracts](../target-architecture/contracts.md) names one anchor: a
commit on `origin/master`'s first-parent chain with a green CI verdict.
`hosts/lib/ci_verdict.py:ci_walk_candidates` already gives the GitOps deployer that anchor, and both
k3s nodes take setup roles from master on it.

No operator-signature check can pass on a master commit. `land.sh` squash-merges through
the REST endpoint, so GitHub commits the merge and signs it with its own key. H3's
*Baseline* records `committer GitHub` and `verified: true` on `70591c307`. Renovate's
commits are authored by `renovate[bot]`.

H3 *Open questions* 1 weighed three options:

1. **(a) Master plus green CI, and nothing more.**
1. **(b) A signed landing tag.** `land.sh` pushes a signed tag for each merge SHA, and the
   builder verifies the tag against a root-owned allowed-signers file.
1. **(c) A Pi-only manual approval gate.** The Pi package ships no enable link for
   `homelab-updater.timer`. The Pi then installs only when the operator starts its updater
   from a LAN session.

Option (b) narrows nothing. The signing key lives on `daniel-box`, and the tick applies the
`homelab_builder` role there from master on anchor (a) (H3.4a). A merged change to that
role therefore reaches the key, whatever the builder checks. The tag protects only if its
key is out of reach of every account that runs `land.sh`. In practice the operator would
then sign each landing, and every host change would wait on the operator.

Option (c) is the only option that narrows what a merged commit reaches, and it narrows it
only for the Pi. Every Pi change, Renovate's included, would then wait on the operator,
and `land.sh` would end `deferred` for the Pi.

## Decision

The builder builds a commit on `origin/master`'s first-parent chain that has a green CI
verdict. That is the C5 anchor the GitOps deployer already uses. The builder checks no
commit signature and no landing tag. Every host takes its packages on that anchor,
`daniel-pi` included.

## Consequences

- **The Pi gains its first unattended path from master to root.** At `70591c307`,
  `daniel-pi.yml` sets `has_gitops: false`, so no deploy reaches the Pi unattended. From
  H3.9b, a merged commit with a green CI verdict reaches root on the Pi within one updater
  fire. The Pi carries the WireGuard endpoint. H3 *Risks* records this cost.
- **The protection is the merge gate itself:** the branch ruleset, CI, and who can merge.
  A compromised merge path reaches every host, the Pi included. That was already true of
  the two k3s nodes through the GitOps deployer.
- **Builder step 5 adds no trust check.** It writes no `reason="trust"` refusal, and H3.5a
  ships no red and green pair for one.
- **Option (c) stays available at no design cost.** H3.9b's enable link for
  `homelab-updater.timer` is the switch. Dropping that link moves the Pi to (c), and the
  operator then meets H3's exit observation 4 with a hand-started run.
- **Reversing to (b) needs a signing key outside every `land.sh` account's reach.** Without
  that, a tag check adds a landing step and protects nothing. Such a key does not exist on
  2026-10-14.

## Governs

Builder step 5 reads the anchor in `choose` in
`hosts/lib/host_package_builder.py`. The `# DECIDED:` marker there
opens "the anchor is a commit on origin/master's first-parent chain" and cites this record.
`governs:` names it by that text rather than by line, so an edit above it moves nothing.

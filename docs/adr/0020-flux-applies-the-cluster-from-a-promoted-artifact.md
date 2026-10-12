---
id: "0020"
title: Flux applies the cluster from a promoted artifact
status: Proposed
date: 2026-10-12
governs: []
---

# ADR-0020: Flux applies the cluster from a promoted artifact

## Status

Proposed. The operator approved starting slice D1 on 2026-10-12. Acceptance is
**operator-only** and is requested in slice D6, which is also where
[ADR-0010](0010-pull-based-gitops-over-argo-and-flux.md) becomes `Superseded by ADR-0020`.
Until then ADR-0010 stays `Accepted`.

## Context

[ADR-0010](0010-pull-based-gitops-over-argo-and-flux.md) kept the homelab's own pull-based
deployer and deferred Flux. It rested on three premises: the deployer's decision logic had no
controller-native equivalent, Ansible had to stay the renderer and the secret plane, and
`daniel-box` had little memory to spare.

The [target architecture](../target-architecture/design.md) re-measured all three, in its
section *Cluster deploy plane*. The deployer's decision logic grew from 1,928 to 9,499 lines.
Five path narrowings exist "because a path moved while the rendered bytes did not"
([#2810](https://github.com/DanielH2018/server/issues/2810)). Flux decrypts age natively.
`daniel-box` read 44% to 54% memory in the D1 plan's readings of 2026-10-11 and 2026-10-12,
where ADR-0010 recorded 65%. These figures are context for the decision at the time, not live
settings.

On 2026-10-12 the operator was asked whether to approve starting D1, which ADR-0010 deferred.
The answer was:

> The Target Architecture docs are the source of truth

That ruling adopts slice D1 as
[planned](../target-architecture/deploy/d1-flux-installed.md). This record is the ruling's
written form, in `Proposed` status, opened by
[#4695](https://github.com/DanielH2018/server/issues/4695).

## Decision

The cluster's deploy plane moves to Flux, which applies a rendered artifact that CI publishes
and a `promote` workflow promotes. [Contracts](../target-architecture/contracts.md) C2 owns the
artifact and its promotion, and C3 owns the Flux objects. The move starts with slice D1,
which installs `source-controller`, `kustomize-controller` and `notification-controller` in
`flux-system`. Those controllers follow `oci://ghcr.io/danielh2018/homelab-manifests:prod`, whose root
Kustomization is empty. Under D1, Flux owns no object outside `flux-system`, and Ansible still
deploys every service. Ownership of a live object moves only in D2 and later slices, under the
two-writer rule (C4).

## Consequences

- **Two deploy planes coexist from D1 to D6.** The GitOps deployer and `deploy.sh` keep every
  service. Flux runs beside them and applies only its own namespace. A reader who finds Flux
  controllers running reads this record for why ADR-0010 still says `Accepted`.
- **Deploys gain a dependency on GitHub and GHCR.** The artifact lives in GHCR, and
  `promote` runs in GitHub Actions. The break-glass promotion in C2 covers an outage of either.
- **Flux's CRDs and controllers become something to upgrade.** The Flux release the plan
  measured lists compatibility only up to Kubernetes 1.36, while the cluster runs 1.37. The D1
  plan's Open question 1 owns that gap.
- **Reversal is cheap until D2.** Flux owns nothing outside `flux-system` before D2's flip, so
  removing the namespace and the CRDs reverses D1 without touching a service.
- **Validation covers the Flux kinds before any manifest renders them.**
  `scripts/validate/refresh_vendored_schemas.py:VENDORED` lists `OCIRepository`, `Kustomization`,
  `Provider` and `Alert`, and `CORE_GROUP_FILES` lists the `apiextensions.k8s.io` group that the
  Flux CRDs belong to.

## Governs

Nothing yet. The flux role that D1's later PRs add carries the first marker that cites this
record, and adds it to `governs:` in the same PR.

# Architecture decisions

Records of decisions that shaped this homelab, and why. A decision that is still in force
lives here; the current *state* it produced lives in the reference and runbook pages.

## When to write one

Write an ADR when the reasoning behind a decision outgrows the line it sits on. Short
reasoning stays where it is, as a `# DECIDED:` comment at the code line it governs — that is
what a reviewer trips over before they spend an hour re-deriving it.

An ADR is the long-form why; the marker is the pointer. Where both exist they reference each
other, and `ansible/tests/test_adr_links.py` fails if either direction breaks.

The marker keeps `# DECIDED:` literal and carries the reference after it —
`# DECIDED: … (ADR-0011)`, never `# DECIDED (ADR-0011):`. The reviewer brief greps the
literal form, so moving the colon hides the marker from the one reader who needs it.

## Superseding

An ADR is never deleted or rewritten to match a reversed decision. Write a new one and set
the old one's status to `Superseded by ADR-NNNN`. The record of a decision that turned out
wrong is worth more than the record of one that did not.

## The records

| ADR | Title | Status | Date |
|---|---|---|---|
| [0001](0001-mkdocs-site-with-generated-reference.md) | A MkDocs site whose reference pages are generated from the Ansible tree | Accepted | 2026-08-24 |
| [0002](0002-k3s-over-docker-compose-for-the-cluster-nodes.md) | k3s replaces Docker Compose on the two cluster nodes | Accepted | 2026-08-01 |
| [0003](0003-sops-with-age-for-secrets-at-rest.md) | Secrets are encrypted at rest with SOPS and age, in the repo | Accepted | 2026-08-01 |
| [0004](0004-authelia-is-the-single-sign-on-layer.md) | Authelia is the single sign-on layer, enforced at the edge | Accepted | 2026-08-01 |
| [0005](0005-traefik-is-the-edge-with-ingressroute-crds.md) | Traefik is the cluster edge, routed by IngressRoute CRDs | Accepted | 2026-08-01 |
| [0006](0006-longhorn-for-cluster-storage.md) | Longhorn provides replicated block storage for the cluster | Accepted | 2026-08-01 |
| [0007](0007-backup-tiering-r2-daily-b2-weekly.md) | Backups are tiered — R2 daily, B2 weekly and sharded across the week | Accepted | 2026-08-16 |
| [0008](0008-16-mib-longhorn-blocks.md) | New Longhorn volumes use 16 MiB blocks | Accepted | 2026-08-16 |
| [0009](0009-networkpolicy-default-deny-ingress.md) | Every pod is fenced by a default-deny ingress NetworkPolicy | Accepted | 2026-08-16 |
| [0010](0010-pull-based-gitops-over-argo-and-flux.md) | The homelab keeps its own pull-based deployer instead of Argo CD or Flux | Accepted | 2026-08-21 |
| [0011](0011-one-lock-serialises-every-deploy-path.md) | One lock serialises every path that writes the git tree | Accepted | 2026-08-23 |
| [0012](0012-zero-downtime-deploys-gate-on-rollout-and-restarts.md) | A deploy is not finished until the rollout completes and the pod stays up | Accepted | 2026-08-20 |
| [0013](0013-daniel-pi-stays-on-docker.md) | daniel-pi stays on Docker and out of the cluster | Accepted | 2026-08-01 |
| [0014](0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | Kopia is retired and Longhorn owns the B2 credentials | Accepted | 2026-08-14 |
| [0015](0015-d2-for-hand-authored-diagrams.md) | A hand-drawn diagram is D2 source in the repo, not Mermaid in the page | Accepted | 2026-08-24 |

## The long-form behind a record

Four decisions were worked out at length before they were compressed into a record. Those
documents stay live under *Background* in the site navigation, because the record states the
decision and the document holds the evidence for it:

| Document | Record |
|---|---|
| [Longhorn backup tiering](../longhorn-backup-tiering.md) | [ADR-0007](0007-backup-tiering-r2-daily-b2-weekly.md), [ADR-0008](0008-16-mib-longhorn-blocks.md) |
| [NetworkPolicy default-deny](../networkpolicy-default-deny.md) | [ADR-0009](0009-networkpolicy-default-deny-ingress.md) |
| [Argo and Flux evaluation](../gitops-argo-flux-evaluation.md) | [ADR-0010](0010-pull-based-gitops-over-argo-and-flux.md) |

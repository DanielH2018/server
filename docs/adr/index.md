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
| [0006](0006-longhorn-for-cluster-storage.md) | Longhorn provides replicated block storage for the cluster | Accepted | 2026-08-01 |
| [0013](0013-daniel-pi-stays-on-docker.md) | daniel-pi stays on Docker and out of the cluster | Accepted | 2026-08-01 |
| [0014](0014-kopia-retired-longhorn-owns-the-b2-credentials.md) | Kopia is retired and Longhorn owns the B2 credentials | Accepted | 2026-08-14 |

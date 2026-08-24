---
id: "0014"
title: Kopia is retired and Longhorn owns the B2 credentials
status: Accepted
date: 2026-08-14
governs: []
---

# ADR-0014: Kopia is retired and Longhorn owns the B2 credentials

## Status

Accepted.

## Context

Kopia backed up host paths to Backblaze B2 for roughly two years and accumulated real
knowledge in the process: its ignore rules encoded what was and was not worth backing up.

Once workload state moved onto Longhorn volumes
([ADR-0006](0006-longhorn-for-cluster-storage.md)), Longhorn's own backup target covered
everything on a persistent volume. Kopia was left backing up progressively less, while still
holding B2 credentials and still costing transactions against the same account cap.

Two tools writing to one B2 account also made spend impossible to attribute. A cap event
could come from either, and neither could see the other's usage.

## Decision

Kopia is retired. Longhorn's backup target is the only writer to B2, and the `kopia_b2_*`
secrets in SOPS are Longhorn's credentials despite the name.

Kopia's ignore rules were not discarded. They were translated into Longhorn's vocabulary
before the retirement — see
[ADR-0007](0007-backup-tiering-r2-daily-b2-weekly.md).

## Consequences

**The SOPS key names lie.** `kopia_b2_*` are Longhorn's credentials. Renaming them means a
rotation, so the names stayed and this record is the explanation. Never attribute B2 spend
to Kopia on the strength of a key name.

**`docs/kopia-disaster-recovery.md` describes a retired tool.** It is kept because the
account, the bucket and the recovery vocabulary are still real, but the tool in its title is
not running.

**Nothing backs up host paths that are not on a Longhorn volume.** That was the coverage
Kopia provided, and retiring it narrowed the backup surface to persistent volumes by
intention. Anything outside a PV is not backed up.

## Governs

No single line. `governs:` is empty; the decision shows up as the absence of a Kopia role.

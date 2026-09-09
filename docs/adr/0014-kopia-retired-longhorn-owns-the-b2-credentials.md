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

Kopia is retired. Longhorn's backup target is the only writer to B2, and the B2 credentials
in SOPS are Longhorn's.

**Amended 2026-09-09: the credentials were renamed `kopia_b2_*` → `longhorn_b2_*`.** The
original decision kept the old names, on the reading that renaming a SOPS key amounts to a
rotation. That reading was half right. A rename does re-encrypt the value — SOPS binds each
ciphertext to its key path — so the rename commit reads as a rotation to
`ciphertext_rotation_dates`, which would have advanced all four dates to the rename and
overstated their freshness by three months. It is not a *real* rotation, though: the
plaintext is untouched, so nothing in B2 or the cluster moves. Recording the old spelling in
`RENAMED_FROM` (`scripts/secrets_mgmt/git_dates.py`) makes the derivation ignore that one
commit, and the registry rows carry the pre-rename dates over by hand. The procedure is in
[`docs/secret-rotation.md`](../secret-rotation.md).

Kopia's ignore rules were not discarded. They were translated into Longhorn's vocabulary
before the retirement — see
[ADR-0007](0007-backup-tiering-r2-daily-b2-weekly.md).

## Consequences

**The SOPS key names said Kopia until 2026-09-09.** They are `longhorn_b2_*` now. Never
attribute B2 spend to Kopia on the strength of a key name — and note that the bucket itself
is still called `daniel-server-kopia`, which no rename touches.

**A SOPS rename is not free.** It re-encrypts, so it needs a `RENAMED_FROM` entry and a
hand-carried `last_rotated` or it silently resets the secret's rotation clock. That cost
generalises to every secret in the store, which is why the procedure landed in
[`docs/secret-rotation.md`](../secret-rotation.md) rather than staying here.

**`docs/kopia-disaster-recovery.md` describes a retired tool.** It is kept because the
account, the bucket and the recovery vocabulary are still real, but the tool in its title is
not running.

**Nothing backs up host paths that are not on a Longhorn volume.** That was the coverage
Kopia provided, and retiring it narrowed the backup surface to persistent volumes by
intention. Anything outside a PV is not backed up.

## Governs

No single line. `governs:` is empty; the decision shows up as the absence of a Kopia role.

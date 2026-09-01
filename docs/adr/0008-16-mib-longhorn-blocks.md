---
id: "0008"
title: New Longhorn volumes use 16 MiB blocks
status: Accepted
date: 2026-08-16
governs: []
---

# ADR-0008: New Longhorn volumes use 16 MiB blocks

## Status

Accepted, and only partly applied — see Consequences.

## Context

Longhorn's default 2 MiB block size sets how many objects a backup writes, how many a restore
reads, and how many a prune has to list. Every axis of Backblaze B2 cost is counted in
transactions, so the block size is the multiplier on all of them at once.

This came up while fitting usage inside caps the operator had declined to raise
([ADR-0007](0007-backup-tiering-r2-daily-b2-weekly.md)). Cadence and exclusions reduce how
often and how much is written; block size reduces the transaction cost of everything that
still is.

The constraint that shapes the decision: `volumeSize` must be a multiple of the block size,
and **the block size is immutable once a volume exists**. There is no in-place migration.

## Decision

New Longhorn volumes are created with 16 MiB blocks. Existing volumes keep whatever they
were created with unless they are recreated deliberately.

## Consequences

**It binds new volumes only.** The field is immutable, so this is not a change that sweeps
the estate — it is a default that takes effect as volumes are replaced. Any statement that
"the homelab uses 16 MiB blocks" is wrong.

**Four volumes stay at 2 MiB on purpose.** The daily R2 set is small and its cost is not
transaction-bound the way the B2 set is, so recreating those volumes would buy nothing.

**A migrated volume needs a relabel step**, which is easy to omit and leaves the volume
outside its tier's RecurringJob.

**PVC sizes must be multiples of the block size**, which is a deploy-time rejection rather
than a warning. Enforced tree-wide by
`ansible/tests/longhorn/test_pvc_sizes_match_block_size.py` — the guard exists because the two block
sizes now coexist and the correct multiple depends on which one a volume has.

## Governs

No single line. `governs:` is empty; the value lives in the Longhorn StorageClass parameters.

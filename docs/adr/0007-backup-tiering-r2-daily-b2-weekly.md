---
id: "0007"
title: Backups are tiered — R2 daily, B2 weekly and sharded across the week
status: Accepted
date: 2026-08-16
governs: []
---

# ADR-0007: Backups are tiered — R2 daily, B2 weekly and sharded across the week

## Status

Accepted. Reached over five Backblaze B2 transaction-cap events between 2026-08-12 and
2026-08-16, and current behaviour since both targets were re-armed on 2026-08-17.

## Context

Backblaze B2 charges per API transaction, and the account has caps. The homelab hit them
five times. The operator declined to raise the caps, which fixed the shape of the problem:
usage had to fit the budget rather than the budget growing to fit usage.

Kopia's path-level ignore rules encoded two years of judgement about what was worth backing
up. Longhorn cannot express a path exclude — it backs up whole block volumes — so every rule
had to be translated rather than carried over. Each translated one of three ways: a
whole-tree exclusion became a whole-volume `no-backup`; a high-churn path became an emptyDir
diversion in the pod spec; everything else became cadence.

The key economic fact is that block-level incrementals make *static* weight nearly free
after the first full backup, because unchanged blocks are never re-uploaded. Churn is what
spends transactions, not size.

A second failure mode was concentration: a single cap-day carrying a batch of volumes is how
a budget that is adequate on average still breaks.

## Decision

Two backup targets, split by what the data is worth and what it costs.

Four volumes go to Cloudflare R2 daily: the TLS material, the SSO store and the two
home-automation stores. They are the whole daily tier, and R2 exists so that these survive a
B2 account-level failure — a cap, a billing problem or a lockout.

Every other backed-up volume goes to B2 weekly, sharded across the seven weekdays by list
index modulo the day of week, roughly three volumes a day, so no single cap-day carries a
batch.

A week-old worst-case restore for the B2 set is an accepted trade.

## Consequences

**The tier is a property of the volume, not of the group.** Read `spec.backupTargetName`;
inferring the target from which group a volume is in gets it wrong.

**Longhorn's retention is per job, not per volume.** Moving a volume between tiers strands
the rest of its old job's set permanently under no retention at all.

**Pruning costs transactions per deleted backup**, at roughly 1.28 list operations per stored
block — so a target must be drained before it is armed, not after. The orphan reaper is worse:
it pays a full prune per stray backup, and seven strays measured about 3,640 class-C
transactions, one and a half times the daily cap.

**A failing target retries at 200 to 260 requests a minute**, ignoring the poll interval.
Blanking the target is what stops it, not waiting.

**Disarming a target makes its monitor permanently red**, so the monitor has to be gated too
— and what was suppressed has to be named, or a real failure hides behind a deliberate one.

**A volume that moves tiers waits a shard week for coverage**, and a seed backup does not
count: the rotation check matches the tier's RecurringJob label, which a seed never carries.
Both are enforced by tests rather than left to memory.

**A cap denial reads as data loss.** A class-B 403 surfaces as "cannot find … in
backupstore," which is indistinguishable from a missing backup until the caps are checked.

## Governs

No single line. The tier lists live in `ansible/roles/setup/k3s/defaults/main.yml`, so
`governs:` is empty.

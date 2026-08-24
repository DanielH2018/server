# Longhorn Disaster Recovery — restore from B2 (and R2)

Recover the cluster's PVC state when **daniel-box is gone** (dead disk, lost host,
total-loss event). Successor to [`kopia-disaster-recovery.md`](kopia-disaster-recovery.md)
(retired 2026-08-13 — the kopia repo was deleted; that doc is kept for the era it
describes). The backupstore lives off-site in B2 under
`s3://daniel-server-kopia@us-east-005/longhorn`, and every credential needed to reach it is
in SOPS — which remains DR-closed exactly as before (age host keys backed up out-of-band +
an off-box recovery recipient), so the capability survives a total loss.

> **Kopia era closed 2026-08-14:** the repo was deleted 08-13, `kopia_password` retired with
> it (8edb11cd), and the residual hidden object versions were hard-purged 08-14 (941
> versions, 4.66 GB) — the bucket now holds `longhorn/` only. The `kopia_b2_*` secrets that
> survive are the B2 *account* credentials, which render the `longhorn-b2` target Secret.

## Two targets: B2 is the default, R2 holds the crown jewels

Since 2026-08-15 (`5ef0dc8e`) there are **two** backup targets, and a restore has to know
which one holds the volume it wants. Routing is per-volume, via `spec.backupTargetName`:

| Target | Longhorn name | Holds | Credential Secret | Rendered from |
|---|---|---|---|---|
| Backblaze B2 | `default` | everything not listed below (weekly, weekday-sharded since 2026-08-16) | `longhorn-b2` | `kopia_b2_key_id` / `kopia_b2_application_key` |
| Cloudflare R2 | `r2` | the four volumes below | `longhorn-r2` | `r2_access_key_id` / `r2_secret_access_key` / `r2_account_id` |

The R2 set (`k3s_longhorn_r2_volumes`) is `homelab/traefik-acme`,
`homelab/authelia-config`, `homelab/home-assistant-config`, `homelab/zigbee2mqtt-data` —
the TLS material every route depends on, the SSO store behind every authenticated route,
and the two home-automation stores that are slow to rebuild by hand. They are on **both**
targets' worth of protection in the sense that matters: a B2 account-level failure (cap,
billing, key revocation) does not take them with it.

To list what is actually routed where, rather than trusting this table:

```bash
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns=VOL:.metadata.name,PVC:.status.kubernetesStatus.pvcName,TARGET:.spec.backupTargetName
```

Arming is independent per target (`k3s_longhorn_backup_armed`, `k3s_longhorn_r2_armed`),
and step 3's cap caveat applies to B2 only — R2's free tier has no transaction cap, and its
own headroom is watched by monitor-bridge's **R2 Free Tier Headroom** monitor.

## The off-site recovery kit (carried over from the kopia era — still the recovery spine)

Recovery has three independent legs: **B2** holds the data, an **out-of-band age key**
decrypts the secrets, and **GitHub** holds the only off-site copy of the encrypted
`secrets.yml` + all the Ansible. The age key alone can't reconstruct `secrets.yml`, so a
simultaneous loss of the hosts *and* the GitHub repo would strand the B2 credentials.
Close that leg by keeping the complete kit in ONE off-site place: the age key plus a repo
bundle refreshed whenever the key backup is (or after a `secrets.yml` change):

```bash
git bundle create "homelab-$(date +%F).bundle" --all
# recover: git clone homelab-YYYY-MM-DD.bundle server   (sops -d needs the age key beside it)
```

`secrets.yml` inside the bundle stays SOPS-encrypted — useless without the age key — so the
bundle is no more sensitive than the GitHub repo; the age key is the part to protect.

## The external dead-man's switch (re-homed 2026-08-14; re-validate at drain close)

The one backstop for a total in-house monitoring death is the external UptimeRobot monitor
(dashboard `https://dashboard.uptimerobot.com/monitors/803270234`, probing
`https://homepage.daniel-hunter.com`). Recorded here because an external SaaS can't be
IaC-managed, so this record is its only audit trail. The kopia-era analysis (operator-
accepted residual: the target is an Authelia-gated 302, so it back-stops host/edge/Authelia
death but NOT a Kuma-only container death) predates the migration — the alert brain now
lives on daniel-box (cluster Kuma) and homepage has a cluster identity, so the *shape* of
the residual has moved even if the acceptance likely still holds. Re-validate the target
choice in the final `/homelab-review` pass; history: `kopia-disaster-recovery.md`.

## What is and isn't in the backupstore

Tiering is deliberate — see [`longhorn-backup-tiering.md`](longhorn-backup-tiering.md) for
the per-volume map and each exclusion's rationale:

- **Daily tier** (the four R2 volumes, retain 14): day-old at worst.
- **Weekly tier** (all 21 B2 volumes since the 2026-08-16 sharding, retain 4): up to a week
  old, each volume on its own weekday (~3/day — B2's 2,500/day transaction caps couldn't
  absorb a batch). Acceptable by design — configs, largely regenerable.

  > **Still building depth — counted against the cluster 2026-08-24.** "retain 4" is the
  > steady state; depth builds one backup per volume per week from the volume's first shard
  > run. The tier is producing: 34 completed backups covering all 21 volumes, oldest
  > 2026-08-19, newest the same morning this was checked. No volume has reached retain 4 yet —
  > 9 sit at one recovery point, 11 at two, one at three — so restoring the least-covered
  > volume still gets you its single shard date rather than "up to a week old."
  >
  > This replaces a note written 2026-08-16 that read "zero — no backup in the weekly tier has
  > ever completed", and a second one saying B2 was disarmed after the seventh transaction-cap
  > event and would stay that way. Both were true when written and are not now: B2 re-armed
  > 2026-08-17 (`k3s_longhorn_backup_armed` in
  > `ansible/roles/setup/k3s/defaults/main.yml`) and the first shards landed 2026-08-19.
  >
  > The frozen `daily-backup` objects that the old note called load-bearing are gone — every
  > backup on the B2 target postdates the re-arm. The reaper's refusal to touch a volume whose
  > current tier has produced nothing
  > (`/usr/local/bin/longhorn-reap-orphan-backups.sh`) no longer has anything to protect here,
  > because every volume's current tier has now produced something.
- **No-backup** (16 volumes): rebuilt, not restored. The notable rebuild paths:
  uptime-kuma (recreate the first-run admin by hand; AutoKuma backfills monitors from the
  static-monitors Secret; history is gone), `scrutiny` (TSDB refills from collector runs),
  Pi-hole (`pihole -g` rebuilds gravity; config is Ansible-rendered), `livesync` (a client
  runs "Rebuild everything" — the vault's source of truth is the markdown on each
  Obsidian device), registry/caches/TSDBs (repopulate on use).
- Restores are **crash-consistent** block snapshots: SQLite DBs recover as-of-last-
  checkpoint via their own journal — same semantics kopia's WAL-exclusion rule accepted.

## Procedure (fresh host, total loss)

1. **Base OS + SOPS onboarding** — as in `ansible/README.md` first-host bring-up: install
   uv, run `ansible/bootstrap.yml` on the new host, add its age pubkey to
   `ansible/.sops.yaml`, `sops updatekeys` from a host that can decrypt (or use the
   off-box recovery key if no host survives), commit, pull.
2. **Cluster bring-up**: `uv run ansible-playbook ansible/k3s-bringup.yml`. Set
   `k3s_longhorn_backup_armed: true` so the B2 target arms (it renders the `longhorn-b2`
   credential Secret from `kopia_b2_key_id`/`kopia_b2_application_key` and points the
   target at the bucket), and `k3s_longhorn_r2_armed: true` for the R2 target — the four
   volumes in the table above restore from *that* one, so a B2-only bring-up leaves them
   with nothing to restore from.
3. **Wait for the backupstore sync** — `kubectl -n longhorn-system get backuptarget`
   `AVAILABLE true`, then Backup CRs appear. **The poll interval is `0`** — polling is
   OFF (`k3s_longhorn_backupstore_poll_interval`, set to 0 on 2026-08-15 because even the
   1h setting exhausted B2's Class-B cap by 11:00), so the sync will not happen on its
   own: force it in the Longhorn UI with Backup → Sync.

   Mind the B2 transaction caps: a full-restore day is exactly when the cap can bite
   again, and the storm ratchet is documented in
   [`b2-transaction-cap-monitoring-gaps.md`](b2-transaction-cap-monitoring-gaps.md).

   **A cap denial does NOT surface as a 403 here.** The denied metadata GET arrives as
   `cannot find volume.cfg in backupstore` — which reads exactly like the backup is
   missing, that is, like data loss, at the worst possible moment. That is what the first
   drill hit (below). If you see it, check the caps in the B2 console **before**
   concluding anything about the backup: stop, blank the target
   (`k3s_longhorn_backup_armed: false` + deploy), and resume after the 00:00 UTC reset.
4. **Restore volumes BEFORE any `deploy.yml`** — deploying first would provision fresh
   empty PVCs under the same names. Restore from the target that holds each volume (the
   table above); in the Longhorn UI the backups are listed per target, so the four R2
   volumes will not appear under `default`. In the UI (or per-backup `Volume` CRs with
   `spec.fromBackup`): restore each backed-up volume under its original PV name, then use
   Longhorn's **Create PV/PVC** with the original namespace/PVC names
   (`homelab/<pvc-name>` — the names in `longhorn-backup-tiering.md`'s table).
5. **Deploy**: `uv run ansible-playbook ansible/deploy.yml`. Workloads bind the existing
   PVCs; no-backup volumes provision empty and rebuild per the list above.
6. **Verify**: `uv run python scripts/probe.py targets` and `health <svc>` for the
   restored tier; `probe.py ha verify-automations` for HA; monitor-bridge's board goes
   green as services come up. Restore the Kuma admin + check tiles last (its DB was
   deliberately not restored).

## Assurance gap (known, narrowing)

kopia's three-tier assurance (snapshot → weekly verify → monthly restore drill) is rebuilt for
Longhorn and, since 2026-08-20, exceeds it on cadence: backups are verified to *complete* (the
backup-plane heartbeat) and to *restore*, one volume per night, rotating over the whole backup
set. What the tiers do not give you is simultaneity — see the nightly drill below for what the
fleet-wide claim actually is.

**The first restore drill ran on 2026-08-15, against `traefik-acme`, and it failed** — not
on the data, but on a B2 Class-B cap already at 100%, which surfaced as
`cannot find volume.cfg in backupstore` (see step 3). The lesson worth carrying is the
masked failure mode, not any conclusion about the backups.

**The retry on 2026-08-16 00:11 UTC passed**: the 2026-08-15 nightly of `traefik-acme`
restored from B2 into a fresh volume in ~21 s, and a probe pod confirmed real data
(`acme.json` 16 KB, certificate present, expected domain matched) before full teardown.
The drill playbook lives at `/home/ubuntu/migration-oneshots/restore-drill.yml`; note a
restore Volume CR needs `spec.backupTargetName` since the v1.12 multi-target change, or
it resolves against the volume's default target.

**Scheduled since 2026-08-19; nightly and rotating since 2026-08-20.**
`/usr/local/bin/longhorn-restore-drill.sh` (k3s role, `longhorn-restore-drill.sh.j2`) runs as root
at 04:10 every night. Each run restores the newest backup of ONE volume into a throwaway volume,
checks the restored filesystem has files and clears a byte floor, then tears everything down. It
resolves the backup, volume and size from the cluster — the hand-run version pinned all three, and
a pinned backup ID dies the day retention deletes it.

**Which volume rotates.** Candidates are every volume carrying a
`recurring-job-group.longhorn.io/*` label other than `no-backup` — the same selector check 4 uses,
and the only one that cannot drift from what the RecurringJobs really select. Each night the
least-recently-*attempted* candidate is drilled, so a full cycle takes one night per candidate (25
on 2026-08-20). Ordering by attempt rather than by success is deliberate: a volume that fails
every drill would otherwise stay the least-recently succeeded forever, be picked every night, and
starve the other 24.

A candidate with no Completed backup is skipped rather than failed — check 4 already pages
per-volume for that, and failing here would burn a rotation slot re-reporting it.

The rotation covers **both targets**. The drill was pinned to a B2 volume until 2026-08-20,
because B2 is the store under a transaction cap and drilling an R2 volume proves Cloudflare
instead. That still holds per-night, and stops mattering once every volume comes up in turn: each
target is proven on the nights its own volumes are drilled. R2 restores are free (10M Class B per
month, zero egress), and the 16 MiB block change of 2026-08-19 cut B2's per-restore cost eightfold
— a whole 25-night cycle now costs less than one day's measured baseline Class B spend.

**Checks 7 and 8 of the backup heartbeat watch it**, and they answer different questions. Check 7
is liveness: the drill writes `/var/lib/longhorn-restore-drill/last-success` only after its data
assertions pass, and check 7 pages when that stamp is missing, unparseable, or older than
`k3s_longhorn_restore_drill_max_age_days` (3 — two tolerated bad nights). It fails closed: a drill
that has never run is reported, not skipped.

Check 8 is coverage, and rotation is what made it necessary — a green check 7 now means one of 25
volumes restored. It reads the candidate list the drill publishes to
`/var/lib/longhorn-restore-drill/candidates` and pages, by name, for any candidate whose
`success/<pvc>` stamp is missing or older than one full cycle plus
`k3s_longhorn_restore_drill_coverage_slack_days`. The grace is measured **per volume**, from the
`seen/<pvc>` marker the drill writes the first time that volume appears as a candidate — so a
fresh deploy does not page for volumes whose turn has not come, and neither does a volume that
joins the backup set later. A rotation-wide start date would flag every such volume the day it
joined.

What is still not covered: each night proves one volume, so at any moment the fleet-wide claim is
"every volume restored within the last cycle," not "every volume restores right now." A full-cluster restore is also still rationed — at 16 MiB blocks
(set 2026-08-19) new volumes cost ~8x less to restore, but existing volumes remain at 2 MiB until
recreated. Lean on the 7-day hidden-version window (`daysFromHidingToDeleting: 7` on the bucket)
if something looks wrong mid-restore.

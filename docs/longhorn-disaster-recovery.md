# Longhorn Disaster Recovery — restore from B2

Recover the cluster's PVC state when **daniel-box is gone** (dead disk, lost host,
total-loss event). Successor to [`kopia-disaster-recovery.md`](kopia-disaster-recovery.md)
(retired 2026-08-13 — the kopia repo was deleted; that doc is kept for the era it
describes). The backupstore lives off-site in B2 under
`s3://daniel-server-kopia@us-east-005/longhorn`, and every credential needed to reach it is
in SOPS — which remains DR-closed exactly as before (age host keys backed up out-of-band +
an off-box recovery recipient), so the capability survives a total loss.

## What is and isn't in the backupstore

Tiering is deliberate — see [`longhorn-backup-tiering.md`](longhorn-backup-tiering.md) for
the per-volume map and each exclusion's rationale:

- **Daily tier** (10 volumes, retain 14): day-old at worst.
- **Weekly tier** (11 volumes, Sundays, retain 4): up to a week old. Acceptable by design
  — configs, largely regenerable.
- **No-backup** (16 volumes): rebuilt, not restored. The notable rebuild paths:
  uptime-kuma (recreate the first-run admin by hand; AutoKuma backfills monitors from the
  static-monitors Secret; history is gone), scrutiny (TSDB refills from collector runs),
  Pi-hole (`pihole -g` rebuilds gravity; config is Ansible-rendered), livesync (a client
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
   `k3s_longhorn_backup_armed: true` so the backup target arms (it renders the
   `longhorn-b2` credential Secret from `kopia_b2_key_id`/`kopia_b2_application_key` and
   points the target at the bucket).
3. **Wait for the backupstore sync** — `kubectl -n longhorn-system get backuptarget`
   `AVAILABLE true`, then Backup CRs appear (poll interval is 1h; force it in the
   Longhorn UI with Backup → Sync). Mind the B2 transaction caps: a full-restore day is
   exactly when the cap can bite again, and the storm ratchet is documented in
   [`b2-transaction-cap-monitoring-gaps.md`](b2-transaction-cap-monitoring-gaps.md) — if
   restores start 403ing, stop, blank the target (`k3s_longhorn_backup_armed: false` +
   deploy), and resume after the 00:00 UTC reset.
4. **Restore volumes BEFORE any `deploy.yml`** — deploying first would provision fresh
   empty PVCs under the same names. In the Longhorn UI (or per-backup `Volume` CRs with
   `spec.fromBackup`): restore each backed-up volume under its original PV name, then use
   Longhorn's **Create PV/PVC** with the original namespace/PVC names
   (`homelab/<pvc-name>` — the names in `longhorn-backup-tiering.md`'s table).
5. **Deploy**: `uv run ansible-playbook ansible/deploy.yml`. Workloads bind the existing
   PVCs; no-backup volumes provision empty and rebuild per the list above.
6. **Verify**: `uv run python scripts/probe.py targets` and `health <svc>` for the
   restored tier; `probe.py ha verify-automations` for HA; monitor-bridge's board goes
   green as services come up. Restore the Kuma admin + check tiles last (its DB was
   deliberately not restored).

## Assurance gap (known, open)

kopia's three-tier assurance (snapshot → weekly verify → monthly restore drill) is not
yet rebuilt for Longhorn: backups are verified to *complete* (the backup-plane heartbeat)
but not to *restore*. Until a drill exists, this runbook is exercised only by real
incidents — treat a first restore as untested and lean on the 7-day hidden-version
window (`daysFromHidingToDeleting: 7` on the bucket) if something looks wrong mid-restore.

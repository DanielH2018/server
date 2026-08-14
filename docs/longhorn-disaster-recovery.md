# Longhorn Disaster Recovery — restore from B2

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

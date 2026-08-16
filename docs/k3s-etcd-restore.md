# Restoring the k3s control plane from an off-box etcd snapshot

**Status: procedure documented, NOT drilled.** Written when the off-box snapshot cron landed
(2026-08-16). Unlike `kopia-disaster-recovery.md`, no restore has been performed from these
snapshots yet — treat every step as needing verification the first time it is used, and update
this file with what actually happened.

## What these snapshots do and do not cover

An etcd snapshot is the cluster's **object store**: Deployments, Services, Secrets, ConfigMaps,
CRs, and the PVC objects with their volume bindings. It is **not** the contents of those volumes.

| Loss | Restored from |
|---|---|
| Cluster objects (a bad delete, a corrupted etcd, daniel-box's disk) | the etcd snapshot, here |
| PVC *contents* (the data inside a volume) | Longhorn backups — same R2 bucket, `longhorn` prefix |

A full rebuild needs both, **etcd first**: restore the objects so the PVCs exist, then restore the
volumes into them.

Redeploying from Ansible is the other recovery path and is usually the better one — it rebuilds
from the repo, which is the source of truth. The snapshot matters for what Ansible does *not*
reproduce: objects that exist live but are not declared (the class the `manifest-prune-check`
cron exists to find), and anything created outside the repo.

## Where the snapshots are

- Local, k3s's own schedule: `/var/lib/rancher/k3s/server/db/snapshots/`, 00:00 and 12:00, 5 retained.
- Off-box, the cron this doc is about: R2, `s3://<r2_bucket>/etcd-snapshots/`, daily at 02:45,
  14 retained. Named `offbox-<node>-<unix-timestamp>`, zstd-compressed.
- Credentials: `/etc/rancher/k3s/etcd-s3.env` on daniel-box (0600 root), rendered by the k3s role
  from the `r2_*` SOPS secrets.

Kuma monitor **Off-box etcd Snapshot** (push, daily) plus the `etcd-snapshot-offbox`
Healthchecks.io check cover the cron. The two become live differently: the Kuma tile appears once
`etcd_snapshot_push_token` exists (added 2026-08-16) and the role is deployed, while the
Healthchecks ping is sent from the first run regardless — but it is discarded until the check is
created by hand in the console, because the slug is sent bare and never with `?create=1`. See
`healthchecks-io-deadman.md` for its schedule and grace.

## Listing what is available

```bash
sudo -i
set -a; . /etc/rancher/k3s/etcd-s3.env; set +a
k3s etcd-snapshot list --s3 \
  --s3-bucket "$ETCD_S3_BUCKET" --s3-endpoint "$ETCD_S3_ENDPOINT" \
  --s3-folder etcd-snapshots --s3-region auto
```

`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` come from the env file, so no key ever goes on the
command line.

## Restoring

Single-server cluster, so this is a `--cluster-reset` restore on daniel-box. **It rolls the whole
cluster back to the snapshot's moment** — anything created since is gone.

```bash
sudo systemctl stop k3s
sudo -i
set -a; . /etc/rancher/k3s/etcd-s3.env; set +a

k3s server \
  --cluster-reset \
  --cluster-reset-restore-path=<snapshot-name-from-the-list> \
  --etcd-s3 \
  --etcd-s3-bucket "$ETCD_S3_BUCKET" --etcd-s3-endpoint "$ETCD_S3_ENDPOINT" \
  --etcd-s3-folder etcd-snapshots --etcd-s3-region auto
```

That command restores and exits — it does not stay running. Then:

```bash
systemctl start k3s
```

**daniel-server (the agent) must then rejoin.** A cluster reset changes the cluster's identity;
the agent's cached credentials no longer match. On daniel-server:

```bash
sudo systemctl stop k3s-agent
sudo rm -rf /var/lib/rancher/k3s/agent/client-*.crt /var/lib/rancher/k3s/agent/client-*.key
sudo systemctl start k3s-agent
```

If it will not rejoin, re-run the documented join:
`uv run ansible-playbook ansible/k3s-bringup.yml -e join_agent=daniel-server`.

## After the restore

1. `kubectl get nodes` — both nodes Ready.
2. `kubectl get pods -A | grep -v Running` — Longhorn's CSI plane comes back before anything
   with a PVC can start.
3. Longhorn UI — volumes attached and healthy. If a volume's data is also lost, restore it from
   the Longhorn backup *after* the PVC object exists, not before.
4. Redeploy from Ansible (`./scripts/deploy.sh`) to reconcile anything the snapshot predates.
5. Run `/usr/local/bin/manifest-prune-check.sh` — a restore can resurrect objects that were
   deliberately retired between the snapshot and now, which is exactly what it detects.

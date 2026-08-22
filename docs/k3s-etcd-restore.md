# Restoring the k3s control plane from an off-box etcd snapshot

**Status, 2026-08-22: the off-box leg is proven; the restore is still NOT drilled.** Written
when the off-box snapshot cron landed (2026-08-16). Unlike `kopia-disaster-recovery.md`, no
restore has been performed from these snapshots — treat every step below as needing
verification the first time it is used, and update this file with what actually happened.

What was verified on 2026-08-22, with `scripts/etcd_restore_drill.sh --list-only` and the runs
that followed it:

- The credentials, bucket and folder work, and `k3s etcd-snapshot list --s3` returns real
  snapshots.
- `offbox-daniel-box-1787366702.zip` — the 02:45 snapshot that day — **downloaded and
  decompressed**. So the nightly cron is producing artefacts that are retrievable and intact
  enough for k3s to open.
- Nothing beyond that. The object graph has never been read back out of one of these snapshots,
  which is the claim a restore actually rests on.

**A scratch restore alongside the running k3s does not work, and is not worth more attempts.**
`k3s server --cluster-reset` assumes it is the only k3s on the host. Five obstacles were found
and the first four fixed — the token file must exist rather than be passed, isolation flags
belong on both invocations, `--disable-agent` leaves the load-balancer on 6444
(`--lb-server-port` moves it), and `--cluster-reset-restore-path` is a name relative to
`<data-dir>/server/db/snapshots` that k3s joins unconditionally, so absolute paths double and
`--etcd-s3` doubles them for you. The fifth is a wedge in "Waiting to retrieve agent
configuration" that ran 17 minutes on 6 seconds of CPU. The script's header records each one.

Two paths finish the job, and neither is more patching:

1. **Run the drill on a host with no k3s of its own** — a throwaway VM. Every obstacle above
   comes from sharing the host, so they all evaporate. This is the cheap one and it stays
   non-destructive.
2. **Take a scheduled outage and do the real restore below.** It proves the most, including the
   agent rejoin and the Longhorn reattach that no scratch drill can exercise.

The isolation itself held: across all five failed runs the live cluster stayed Ready, both nodes
included, and every write landed under `/var/tmp`.

## What these snapshots do and do not cover

An etcd snapshot is the cluster's **object store**: Deployments, Services, Secrets, ConfigMaps,
CRs, and the PVC objects with their volume bindings. It is **not** the contents of those volumes.

| Loss | Restored from |
|---|---|
| Cluster objects (a bad delete, a corrupted etcd, daniel-box's disk) | the etcd snapshot, here |
| PVC *contents* (the data inside a volume) | Longhorn backups — same R2 bucket, `longhorn` prefix |

A full rebuild needs both, **etcd first**: restore the objects so the PVCs exist, then restore the
volumes into them.

### Since 2026-08-20, a snapshot alone cannot give you the Secrets back

`--secrets-encryption` is armed (`k3s_secrets_encryption` in the k3s role), so Secrets are stored
in etcd encrypted with a key held at `/var/lib/rancher/k3s/server/cred/encryption-config.json` on
daniel-box. That key is **not in the snapshot** and is not in this repo.

- **Restoring onto daniel-box with its disk intact**: nothing changes, the key is already there.
- **Restoring onto a rebuilt or replacement host**: you must put the same
  `encryption-config.json` in place *before* starting k3s, or every Secret in the restored
  cluster decodes to garbage. The objects restore fine and the failure surfaces later, as pods
  that cannot read their credentials — which is the worst time to discover it.

This is the trade the encryption buys: the daily snapshot in R2 no longer carries every homelab
credential in plaintext, and in exchange the key becomes a second thing that has to survive
daniel-box. Keep a copy somewhere that is not daniel-box's disk and not the R2 bucket the
snapshot is in — putting the key next to the snapshot restores exactly the exposure the
encryption removed.

Snapshots taken **before** 2026-08-20 predate the change and restore without the key.

Redeploying from Ansible is the other recovery path and is usually the better one — it rebuilds
from the repo, which is the source of truth. The snapshot matters for what Ansible does *not*
reproduce: objects that exist live but are not declared (the class the `manifest-prune-check`
cron exists to find), and anything created outside the repo.

## Where the snapshots are

- Local, k3s's own schedule: `/var/lib/rancher/k3s/server/db/snapshots/`, 00:00 and 12:00, 5 retained.
- Off-box, the cron this doc is about: R2, `s3://<r2_bucket>/etcd-snapshots/`, daily at 02:45,
  14 retained. Named `offbox-<node>-<unix-timestamp>.zip` — compressed, and the extension is
  literally `.zip`, not zstd as this line claimed until 2026-08-22. The name matters: it is what
  `--cluster-reset-restore-path` takes, and it takes it as a NAME, never a path (see below).
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
5. Run `sudo /usr/local/bin/manifest-prune-check.sh` — a restore can resurrect objects that were
   deliberately retired between the snapshot and now, which is exactly what it detects. The
   staged manifest dirs it diffs against are mode 0700, so a non-`sudo` run now fails fast
   (`exit 64`, "must run as root") rather than doing anything useful — it needs root to see the
   real manifest set at all.

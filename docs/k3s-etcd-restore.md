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

### Since 2026-08-20, a snapshot alone cannot give you the Secrets back — the CLUSTER TOKEN is what must survive

`--secrets-encryption` is armed (`k3s_secrets_encryption` in the k3s role), so Secrets are stored
in etcd encrypted with a key held at `/var/lib/rancher/k3s/server/cred/encryption-config.json` on
daniel-box.

**Corrected 2026-08-23: that key IS in the snapshot, and this section previously said it was
not.** k3s stores the bootstrap set's file *contents* in the datastore under `/bootstrap`,
encrypted with the cluster token — `ControlRuntimeBootstrap` includes `EncryptionConfig`
(`pkg/daemons/config/types.go`), `ReadFromDisk` reads each path's bytes into `File.Content`
(`pkg/bootstrap/bootstrap.go`), and `pkg/cluster/storage.go` describes the blob as "CA certs and
keys, encryption passphrases, etc — encrypted with the join token." `secrets-encrypt rotate-keys`
calls `cluster.Save`, so the stored copy tracks rotations. Verified against k3s v1.36.3+k3s1, the
running version. An etcd snapshot is an unfiltered datastore image, so it carries that blob.

**The artifact that has to survive daniel-box is therefore `/var/lib/rancher/k3s/server/token`,
not `encryption-config.json`.** Without the token nothing in the blob decrypts — CA certs, service
key and encryption config alike — so a rebuilt host restores *nothing*, not merely Secrets. It is
not in this repo: `grep -ci k3s ansible/vars/secrets.yml` returns 0, and `k3s-bringup.yml` slurps
it off daniel-box's own disk at join time.

**Out-of-band copy taken 2026-08-23** — the operator's password manager. That is the whole fix for
the failure mode, and it is deliberately not machine-checkable: no automated check asserts the
copy still exists, so this dated line is the only record. Re-verify by hash rather than by eye,
which exposes nothing:

```bash
sudo sha256sum /var/lib/rancher/k3s/server/token   # compare against the saved copy's hash
```

The token changes only on a deliberate rotation, so the copy stays valid until one happens. **If
you ever rotate it, this line is stale the moment you do** — re-copy first, then update the date.

- **Restoring onto daniel-box with its disk intact**: nothing changes, everything is already there.
- **Restoring onto a rebuilt or replacement host**: supply the token. `--cluster-reset` reads it
  from `<data-dir>/server/token` and checks for that FILE — `--token-file` does not satisfy it
  (measured twice, 2026-08-22; see `scripts/etcd_restore_drill.sh`).

This is the trade the encryption buys: the daily snapshot in R2 no longer carries every homelab
credential in plaintext, and in exchange the token becomes a second thing that has to survive
daniel-box. Keep it somewhere that is neither daniel-box's disk nor the R2 bucket the snapshot is
in — putting the token next to the snapshot restores exactly the exposure the encryption removed.

Snapshots taken **before** 2026-08-20 predate the change and restore without any of this.

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

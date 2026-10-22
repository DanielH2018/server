# Longhorn Disaster Recovery — restore from B2 (and R2)

Recover the cluster's PVC state when **daniel-box is gone** (dead disk, lost host,
total-loss event). The backupstore lives off-site in B2 under
`s3://daniel-server-kopia@us-east-005/longhorn` (the bucket kept its name from the retired
kopia setup), and every credential needed to reach it is
in SOPS — which remains DR-closed (age host keys backed up out-of-band +
an off-box recovery recipient), so the capability survives a total loss.

## Two targets: B2 is the default, R2 holds the crown jewels

Since 2026-08-15 (`5ef0dc8e`) there are **two** backup targets, and a restore has to know
which one holds the volume it wants. Routing is per-volume, via `spec.backupTargetName`:

| Target | Longhorn name | Holds | Credential Secret | Rendered from |
|---|---|---|---|---|
| Backblaze B2 | `default` | everything not listed below (weekly, weekday-sharded since 2026-08-16) | `longhorn-b2` | `longhorn_b2_key_id` / `longhorn_b2_application_key` |
| Cloudflare R2 | `r2` | the four volumes below | `longhorn-r2` | `r2_access_key_id` / `r2_secret_access_key` / `r2_account_id` |

The R2 set (`k3s_longhorn_r2_volumes`) is `homelab/traefik-acme`,
`homelab/authelia-config`, `homelab/home-assistant-config`, `homelab/zigbee2mqtt-data`.
The k3s role derives it from the `backup_claims` of every `home-critical` entry (tier
`home-edge` or `home-automation`) in
`containers_list`, so a change to the set is an edit to those entries. The four volumes are
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

## The off-site recovery kit

Recovery has three independent legs: **B2** holds the data, an **out-of-band age key**
decrypts the secrets, and **GitHub** holds the only off-site copy of the encrypted
`secrets.yml` + all the Ansible. The age key alone can't reconstruct `secrets.yml`, so a
simultaneous loss of the hosts *and* the GitHub repo would strand the B2 credentials.
Close that leg by keeping the complete kit in ONE off-site place: the age key plus a repo
bundle refreshed whenever the key backup is (or after a `secrets.yml` change), with the
`claude` issue register exported beside it:

```bash
git bundle create "homelab-$(date +%F).bundle" --all
uv run python scripts/dev/findings.py export --out "homelab-register-$(date +%F).json"
# recover: git clone homelab-YYYY-MM-DD.bundle server
#   then, operator-only: sops -d needs the age key beside it, and prints every secret
```

`secrets.yml` inside the bundle stays SOPS-encrypted — useless without the age key — so the
bundle is no more sensitive than the GitHub repo; the age key is the part to protect.

The register export exists because a bundle carries git history only. Every refuted and
accepted finding lives in a GitHub issue, and losing GitHub would lose those rulings, so the
next review would re-file them. The export holds every `claude` issue in every state with
its body, comments, labels and closing PRs. It reads the register in `created:` date slices
because gh's label-filtered list stops at 1000 issues; a slice that reaches the cap is split
in half, and a single day at the cap fails the export rather than writing a short file. It
takes about a minute per 1000 issues.

## The external dead-man's switch

The one backstop for a total in-house monitoring death is the external UptimeRobot monitor
**`Auth Health`**, a case-sensitive keyword monitor that requires `"status":"OK"` from
`https://auth.daniel-hunter.com/api/health`. UptimeRobot is an external SaaS that no IaC
manages, so [`uptime-robot-monitors.md`](uptime-robot-monitors.md) is its audit trail: it
holds the monitor ids, why this target replaced the earlier one, and the residual that
remains.

## What is and isn't in the backupstore

Tiering is deliberate — see [`longhorn-backup-tiering.md`](longhorn-backup-tiering.md) for
the per-volume map and each exclusion's rationale:

<!-- Generated from the k3s role's Longhorn defaults; edit those. -->
--8<-- "assets/generated/fragments/longhorn-tiers.md"

- **Daily tier** (the R2 volumes): day-old at worst.
- **Weekly tier** (the B2 volumes, weekday-sharded since 2026-08-16): up to a week
  old, each volume on its own weekday (~3/day — B2's 2,500/day transaction caps couldn't
  absorb a batch). Acceptable by design — configs, largely regenerable.
  A volume added to the tier recently holds fewer recovery points than the retain until it
  has run that many weeks.
- **No-backup** (16 volumes): rebuilt, not restored. The notable rebuild paths:
  uptime-kuma (recreate the first-run admin by hand; AutoKuma backfills monitors from the
  static-monitors Secret; history is gone), `scrutiny` (TSDB refills from collector runs),
  Pi-hole (`pihole -g` rebuilds gravity; config is Ansible-rendered), `livesync` (a client
  runs "Rebuild everything" — the vault's source of truth is the markdown on each
  Obsidian device), registry/caches/TSDBs (repopulate on use).
- Restores are **crash-consistent** block snapshots: SQLite DBs recover as-of-last-
  checkpoint via their own journal.

## Procedure (fresh host, total loss)

1. **Base OS + SOPS onboarding** — as in `ansible/README.md` first-host bring-up: install
   uv, run `ansible/bootstrap.yml` on the new host, add its age pubkey to
   `ansible/.sops.yaml`, `sops updatekeys` from a host that can decrypt (or use the
   off-box recovery key if no host survives), commit, pull.
2. **Cluster bring-up**: `uv run ansible-playbook ansible/k3s-bringup.yml`. Set
   `k3s_longhorn_backup_armed: true` so the B2 target arms (it renders the `longhorn-b2`
   credential Secret from `longhorn_b2_key_id`/`longhorn_b2_application_key` and points the
   target at the bucket), and `k3s_longhorn_r2_armed: true` for the R2 target — the four
   volumes in the table above restore from *that* one, so a B2-only bring-up leaves them
   with nothing to restore from.
   **Order from H1.2.** `cluster-base` (the PriorityClasses and the read-only identity) is
   Flux-owned, and two of its objects are needed before Flux can run. Flux's controllers name
   `homelab-critical`, and `setup/k3s`'s `kubeconfig.yml` reads the token Secret. So step 2
   runs in the order below on a new cluster, and the root `homelab` Kustomization stays
   suspended until the volumes are restored (step 4). Once the root runs, every Flux-owned
   stateful tag provisions fresh empty PVCs under the backed-up names. Steps (a), (c) and (g)
   run playbooks or patches that need `become`, so the operator runs them.

    - (a) `uv run ansible-playbook ansible/k3s-bringup.yml --skip-tags kubeconfig`. It installs
      k3s, and k3s creates its bundled CoreDNS. The play skips the token wait, which would fail
      here.
    - (b) **Operator-only.** Render `cluster-base` and apply it under `kustomize-controller`'s own
      field manager, so Flux's first apply finds its own fields:
      `uv run python scripts/renderer/homelab_render.py cluster --out <out> --role cluster-base`,
      then
      `sudo k3s kubectl apply --server-side --field-manager=kustomize-controller -k <out>/cluster/cluster-base`.
      The directory substitutes nothing, so the placeholder-mode render applies as it stands.
    - (c) `uv run ansible-playbook ansible/k3s-bringup.yml --tags kubeconfig`. The token Secret
      exists, so the bounded wait passes.
    - (d) Run the `flux` role with `flux_sync_suspend: true`, which renders `suspend: true` on
      the `OCIRepository` `homelab` and on the root Kustomization. Its pods admit, because (b)
      created `homelab-critical`. Then, **operator-only**, resume the `OCIRepository` alone so the
      children have a source:
      `sudo k3s kubectl -n flux-system patch ocirepository homelab --type merge --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'`.
      Do not run the `flux` role again before (g): a run with the role's defaults resumes the
      root before the restores.
    - (e) **Operator-only.** Create `flux-system/sops-age` from the password manager (item 10
      of the recovery kit in `docs/break-glass.md`).
    - (f) **Operator-only.** Hold promotions and fix the revision. Wait until no run of the
      `promote` workflow is queued or in progress. Read `prod`'s commit from the
      `org.opencontainers.image.revision` key of `prod`'s manifest in GHCR. When `DEPLOY_HOLD`
      reads `none`, set `gh variable set DEPLOY_HOLD --body dr-restore -R DanielH2018/server`.
      A merge during the restores would otherwise promote, time out in `verify` with the root
      suspended, and reset `prod` to `last-good`. Then render the plumbing tags at that commit
      and apply their `flux/<tag>.yaml` with
      `sudo k3s kubectl apply --server-side --field-manager=kustomize-controller -f <file>`.
      That is the root's own field manager, so the root adopts them. Wait until each reads
      `Ready`.
    - (g) After the restores of step 4, **operator-only**, resume the root with the same patch
      form:
      `sudo k3s kubectl -n flux-system patch kustomization homelab --type merge --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'`.
      Once every status on `prod`'s commit reads `success`, clear the hold with
      `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.

   Before H1.2 there is no step (b): k3s bring-up applies the PriorityClasses and the identity
   itself. Steps 3 and 4 below sit between (f) and (g). D5's flux PR replaces this block with a
   pointer to its own bootstrap section.
3. **Wait for the backupstore sync** — `kubectl -n longhorn-system get backuptarget`
   `AVAILABLE true`, then Backup CRs appear. **The poll interval is `0`** — polling is
   OFF (`k3s_longhorn_backupstore_poll_interval` is 0, because even a 1h interval exhausted
   B2's Class-B cap), so the sync does not happen on its
   own: force it in the Longhorn UI with Backup → Sync.

   Mind the B2 transaction caps: a full-restore day is exactly when the cap can bite
   again, and the storm ratchet is documented in
   [`b2-transaction-cap-monitoring-gaps.md`](archive/b2-transaction-cap-monitoring-gaps.md).

   **A cap denial does NOT surface as a 403 here.** The denied metadata GET arrives as
   `cannot find volume.cfg in backupstore` — which reads exactly like the backup is
   missing, that is, like data loss, at the worst possible moment. That is what the first
   drill hit (below). If you see it, check the caps in the B2 console **before**
   concluding anything about the backup: stop, blank the target
   (`k3s_longhorn_backup_armed: false` + deploy), and resume after the 00:00 UTC reset.
4. **Restore volumes BEFORE any deploy** — deploying first would provision fresh
   empty PVCs under the same names. Before the first restore, run the gates: both targets
   armed and available, a `BackupVolume` on each (the sync happened), and no backed-up PVC
   name already bound to an empty volume. They run as one script, in order, and the exit code
   names the first gate that refused (#2216, the shape `docs/k3s-upgrade.md` set):

   ```bash
   uv run python scripts/deploy_tools/runbook_gates.py longhorn-dr
   ```

   Exit 0 means all three passed; exit 1–3 is the gate that failed, and the script prints
   what it found. Exit 3 on a cluster that already carries the volumes is the right answer:
   this runbook is for a cluster that has lost them. The B2 cap is not something the script
   can see — a cap denial reads as step 3's `volume.cfg` message, and the gates stop on the
   sync rather than on the cap.

   Restore from the target that holds each volume (the
   table above); in the Longhorn UI the backups are listed per target, so the four R2
   volumes do not appear under `default`. In the UI (or per-backup `Volume` CRs with
   `spec.fromBackup`): restore each backed-up volume under its original PV name, then use
   Longhorn's **Create PV/PVC** with the original namespace/PVC names
   (`homelab/<pvc-name>` — the names in `longhorn-backup-tiering.md`'s table).

   If you restored etcd first (`k3s-etcd-restore.md`, for lost cluster objects), the snapshot
   already carries the PV and PVC objects with their volume bindings. It also carries
   Longhorn's own CRs, and that changes what two gates read:

   - **Gate 3 refuses on every original volume.** Each PVC comes back bound to its original
     Longhorn `Volume` CR, which has no `fromBackup`. The script reports such a volume as
     `bound to its original volume`, not as `provisioned empty`. A volume that is healthy
     kept its replicas and needs no restore. A faulted one has lost its data: delete it in
     the Longhorn UI, then restore the backup under the same name so the snapshot's PV binds
     to it.
   - **Gate 2 can pass without a sync.** The snapshot's `BackupVolume` CRs satisfy it, and
     they list the backups that existed when the snapshot was taken. Force Backup → Sync
     anyway, so a backup pruned since then is not offered for a restore.

   Use **Create PV/PVC** only for a volume whose objects the snapshot lacks. No drill has run
   this etcd-first order; the steps above are inferred from what the snapshot holds.
5. **Deploy**: `./scripts/deploy.sh`. Workloads bind the existing
   PVCs; no-backup volumes provision empty and rebuild per the list above.
6. **Verify**: `uv run python scripts/diagnostics/probe.py targets` and `health <svc>` for the
   restored tier; `probe.py ha verify-automations` for HA; monitor-bridge's board goes
   green as services come up. Restore the Kuma admin + check tiles last (its DB was
   deliberately not restored).

## After a whole-cluster cold start (both nodes down at once)

A whole-cluster cold start fails every replica of every attached volume, and Longhorn
auto-salvages them. Accepted: auto-salvage is the designed recovery, and on 2026-10-08 every
salvaged volume returned to `healthy`.
The hazard that follows is a weekly shard's missed run, which this section covers.

**The 2026-10-08 measurement (#3892).** Both nodes rebooted on 2026-10-05: daniel-box at 23:55:01,
daniel-server at 23:59:26. The k3s API stayed down for about 70 h (#3882). The reboot killed
every instance-manager process. Instance-manager pods run with `restartPolicy: Never`, and nothing
could restart them while the API was down. When the API returned at 21:55, longhorn-manager found
both pods `in phase Failed` and recreated them. It then marked every engine and replica those
pods had hosted as ERROR (`shouldn't contain the running instance`). Loki shows 39 volumes
auto-salvaged between 21:56:24 and 21:56:29. The 21:55:24 failures in Loki record when Longhorn
noticed the loss, not when the replicas stopped. Loki ran in the cluster too, so it holds no
lines from inside the outage.

**Why a weekly volume can miss its backup for a week.** The CronJob controller fires each missed
RecurringJob once on recovery. On 2026-10-08 it created the d2, d3 and d4 Jobs at 21:55:20. Each
Job filtered its volumes at 21:55:26. Any volume already marked `faulted` was skipped
(`Cannot create job for <vol> volume in state attached`). Volumes not yet marked were backed up
normally, even while being salvaged. Longhorn does not retry a skipped volume, and the Job still
reports `succeeded=1`. Three weekly volumes were skipped: prowlarr-config (d4), valheim-config
(d2) and karakeep-data (d3). At the time, the backup-health check flagged a skipped weekly volume
only when its newest backup passed `k3s_longhorn_weekly_backup_max_age_hours` (198 h). For
prowlarr that was 2026-10-09 10:40, 13 h after the skip. A volume in the daily tier recovers on
the next night's run.

**How the skip surfaces (#3968).** Check 11 of `longhorn-backup-health.sh` reads the logs of each
RecurringJob's newest pod on every 10-minute tick. It turns the `k3s Longhorn Backup` tile DOWN
for each skipped volume in the weekly tier that has no backup newer than the skip. Any backup
clears it, including a seed. Longhorn keeps one pod per RecurringJob, so the evidence lasts until
that shard's next run replaces the pod, a week later. Skips in the daily tier are not flagged,
because the next night's run closes them.

**What to do after a cold start.** Do not seed every weekly volume whose backup predates the
recovery. Most of them belong to shards that ran normally before the outage, and seeding them
spends the day's B2 budget. Seed only the weekly volumes a catch-up Job skipped. Check 11 names
them as `weekly volume(s) a RecurringJob skipped`. The tile shows only the top-ranked problem,
and after a cold start another problem usually outranks this one, so read the full list with
`journalctl -t longhorn-backup-health`. The Jobs log each skip, so this query also names them,
including skips in the daily tier and skips whose pod has since been replaced:

```bash
uv run python scripts/diagnostics/probe.py loki-query --since 24h \
  '{namespace="longhorn-system"} |= "Cannot create job for"'
```

Seed only the names that carry a `recurring-job-group.longhorn.io/weekly-backup-d*` label. A
daily volume in that list recovers on its own the next night. When Loki does not cover the
recovery window, a weekly volume whose newest backup is more than 168 h old missed its run:

```bash
kubectl get volumes.longhorn.io -n longhorn-system -o json | jq -r '.items[]
  | select(.metadata.labels | keys | any(test("^recurring-job-group.longhorn.io/weekly")))
  | (.status.lastBackupAt // "") as $at
  | select($at == "" or ($at | fromdateiso8601 < now - 168*3600))
  | [(if $at == "" then "never" else $at end), .status.kubernetesStatus.pvcName] | @tsv'
```

Longhorn writes an empty `lastBackupAt`, not null, on a volume with no backup, and
`fromdateiso8601` rejects the empty string. The filter therefore tests for it first and prints
such a volume as `never`.

Seed each one, one at a time. Before you seed more than a few, read the budget line in
`journalctl -t longhorn-backup-health`, as the playbook's header says:

```bash
uv run ansible-playbook ansible/seed_volume_backup.yml -i ansible/inventory/hosts.ini \
  -e seed_claim=<pvc-name> -e seed_allow_existing=true
```

## A stale iSCSI session blocks an attach

**The symptom (#5826).** A pod loops in `ContainerCreating`, and Longhorn deletes it about
every 30 s through `auto-delete-pod-when-volume-detached-unexpectedly`. The pod churn hides the
cause. The instance-manager on the attaching node logs the decisive line:

```text
Failed to init frontend ... failed to logout target ... iscsiadm: initiator reported error (32 - target likely not connected)
```

**The cause.** A replaced instance-manager leaves the iSCSI sessions it served in the node's
kernel. Each one stays in state `FREE` with a `failed` connection. The next time its volume
attaches on that node, the engine's frontend init tries to log the old session out, gets
error 32, and exits. On 2026-10-21 the instance-manager that daniel-server replaced at
2026-10-18T17:48:11Z had left sessions 2 and 14 behind. They blocked Alertmanager's and
Grafana's volumes when Flux took over observability (#5816).

**How it surfaces.** The node-exporter DaemonSet's `iscsi-sessions` sidecar writes one
`homelab_iscsi_session_info` series per session on each k3s node, read from
`/sys/class/iscsi_session/*/state`. `IscsiSessionStale` fires for a session that has not been
`LOGGED_IN` for 15 minutes, and its labels name the node, the session and the target. A
stale session alerts before anything tries to attach its volume. The rule is in
`ansible/roles/k8s/observability/files/rules/host-checks.yaml`.

**The cleanup.** The operator runs these steps over SSH on the node the alert names. They
need root. The sequence uses the standard open-iscsi commands, and it was not yet run against
a live stale session when this section was written. `<sid>` is the number in the alert's
`session` label (`session2` gives `2`), and `<iqn>` is its `target` label.

1. Confirm that the session's volume is not attached on this node. The volume name is the
   part of `<iqn>` after `io.longhorn:`. A volume that is detached, or attached to the other
   node, is safe to clean up:

    ```bash
    kubectl -n longhorn-system get volumes.longhorn.io <pvc-…> \
      -o jsonpath='{.status.state} {.status.currentNodeID}{"\n"}'
    ```

    If the volume is attached on this node, scale its workload to 0 and wait for `detached`
    first.

2. List the SCSI disks behind the session, then delete each one so that no block device
   still points at the dead session:

    ```bash
    sudo iscsiadm -m session -r <sid> -P 3 | grep 'Attached scsi disk'
    echo 1 | sudo tee /sys/block/<sdX>/device/delete
    ```

3. Log out of this one session, then delete the target's node record so that `iscsid` never
   logs back in to it. `-r <sid>` limits the logout to this session, where a logout by
   target would also end a live session to the same target:

    ```bash
    sudo iscsiadm -m session -r <sid> -u
    sudo iscsiadm -m node -T <iqn> -o delete
    ```

    The logout can fail with the same error 32 the engine hit. Run the `-o delete` anyway,
    then go on to step 4.

4. Check that `/sys/class/iscsi_session/session<sid>` is gone. The alert clears within a
   minute or two, after the sidecar writes again and Prometheus scrapes the file.

If the session survives these steps, drain the node and reboot it. A reboot clears every
kernel session.

## Assurance gap (known, narrowing)

Backups are verified to *complete* (the backup-plane heartbeat) and to *restore*, one volume
per night, rotating over the whole backup set. What the tiers do not give you is
simultaneity. See the nightly drill below for what the fleet-wide claim actually is.

The first restore drill (2026-08-15, `traefik-acme`) failed on a B2 Class-B cap, not on the
data, and the cap surfaced as `cannot find volume.cfg in backupstore` (step 3). The retry on
2026-08-16 passed. A restore `Volume` CR needs `spec.backupTargetName`, or it resolves
against the volume's default target.

**Scheduled since 2026-08-19; nightly and rotating since 2026-08-20.**
`/usr/local/bin/longhorn-restore-drill.sh` (k3s role, `longhorn-restore-drill.sh.j2`) runs as root
at 04:10 every night. Each run restores the newest backup of ONE volume into a throwaway volume,
checks the restored filesystem has files and clears a byte floor, then tears everything down. It
resolves the backup, volume and size from the cluster — the hand-run version pinned all three, and
a pinned backup ID dies the day retention deletes it.

**Which volume rotates.** Candidates are every volume carrying a
`recurring-job-group.longhorn.io/*` label other than `no-backup` — the same selector check 4 uses,
and the only one that cannot drift from what the RecurringJobs really select. Each night the
least-recently-*attempted* candidate is drilled, so a full cycle takes one night per candidate.
The drill writes the candidates to `/var/lib/longhorn-restore-drill/candidates`, one per line, so
`wc -l` on that file gives the cycle length in nights. Ordering by attempt rather than by success
is deliberate: a volume that fails every drill would otherwise stay the least-recently succeeded
forever, be picked every night, and starve every other candidate.

A candidate with no Completed backup is skipped rather than failed — check 4 already pages
per-volume for that, and failing here would burn a rotation slot re-reporting it.

The rotation covers **both targets**. The drill was pinned to a B2 volume until 2026-08-20,
because B2 is the store under a transaction cap and drilling an R2 volume proves Cloudflare
instead. That still holds per-night, and stops mattering once every volume comes up in turn: each
target is proven on the nights its own volumes are drilled. R2 restores are free (10M Class B per
month, zero egress), and the 16 MiB block change of 2026-08-19 cut B2's per-restore cost eightfold
— a whole cycle now costs less than one day's measured baseline Class B spend.

**Checks 7 and 8 of the backup heartbeat watch it**, and they answer different questions. Check 7
is liveness: the drill writes `/var/lib/longhorn-restore-drill/last-success` only after its data
assertions pass, and check 7 pages when that stamp is missing, unparseable, or older than
`k3s_longhorn_restore_drill_max_age_days` (3 — two tolerated bad nights). It fails closed: a drill
that has never run is reported, not skipped.

Check 8 is coverage, and rotation is what made it necessary — a green check 7 now means one
candidate volume restored. It reads the candidate list the drill publishes to
`/var/lib/longhorn-restore-drill/candidates` and pages, by name, for any candidate whose
`success/<pvc>` stamp is missing or older than one full cycle plus
`k3s_longhorn_restore_drill_coverage_slack_days`. The grace is measured **per volume**, from the
`seen/<pvc>` marker the drill writes the first time that volume appears as a candidate — so a
fresh deploy does not page for volumes whose turn has not come, and neither does a volume that
joins the backup set later. A rotation-wide start date would flag every such volume the day it
joined.

The candidate list is written after the drill's `actualSize` cap
(`k3s_longhorn_restore_drill_max_actual_bytes`), so a volume that grows past the cap would leave
the rotation and check 8 together. The drill therefore also writes
`/var/lib/longhorn-restore-drill/excluded_oversize`, one `<pvc>\t<actualSize>` line per backed-up
volume the cap keeps out, and the heartbeat pages naming each one. The page clears on the next
drill run after the cap is raised, the volume's snapshots are pruned and trimmed, or the volume
moves to `no-backup`.

The drill also restores into a PVC in the source volume's namespace, so it skips a backed-up
volume whose `status.kubernetesStatus.pvcName` is empty, such as a released PVC whose volume is
still in a backup group. It writes each one to `/var/lib/longhorn-restore-drill/excluded_nopvc`,
one volume name per line, and the heartbeat pages naming it. A volume that is both over the cap
and unbound is listed only in `excluded_oversize`. The page clears on the next drill run after a
PVC is bound to the volume again, the volume is deleted, or it moves to `no-backup`.

**The stamps have a second store, read first.** Checks 7 and 8, gate 2 of
`docs/longhorn-upgrade.md` and the drill's row of `docs/reference/state.md` each read the
ConfigMap `longhorn-restore-drill-stamps` in `k3s_longhorn_restore_drill_namespace` (`homelab`)
before the directory. Each ConfigMap key stands for one file:

| ConfigMap key | File under `/var/lib/longhorn-restore-drill/` | Value |
|---|---|---|
| `last-success` | `last-success` | the file's content, an epoch |
| `candidates` | `candidates` | the file's content, one PVC per line |
| `excluded_oversize` | `excluded_oversize` | the file's content |
| `excluded_nopvc` | `excluded_nopvc` | the file's content |
| `seen.<pvc>` | `seen/<pvc>` | the epoch the marker's mtime held |
| `success.<pvc>` | `success/<pvc>` | the epoch the file held |
| `attempts.<pvc>`, `retries.<pvc>` | `attempts/<pvc>`, `retries/<pvc>` | the epoch the marker's mtime held; only the drill reads these |

While the ConfigMap is absent or holds no `last-success`, every reader reads the directory. A
failed ConfigMap read never falls back: the heartbeat reports it as a failed fetch, the upgrade
gate exits 69, and the state page marks the row unreadable. Two rules apply only to the
ConfigMap. A candidate with no `seen.<pvc>` key takes the ConfigMap's `creationTimestamp` as its
seen time, so check 8 flags it once the coverage window passes instead of skipping it. Check 8
also pages at once, in either store, when the candidate list is non-empty and no candidate has a
seen entry, because that means the drill's state did not persist. Nothing writes the ConfigMap
until the drill moves to it (H2.3c of `docs/target-architecture/host/h2-shrink-host-plane.md`).

What is still not covered: each night proves one volume, so at any moment the fleet-wide claim is
What is still not covered: each night proves one volume, so at any moment the fleet-wide claim is
"every volume restored within the last cycle," not "every volume restores right now." A full-cluster restore is also still rationed: at 16 MiB blocks
(set 2026-08-19) new volumes cost ~8x less to restore, but existing volumes remain at 2 MiB until
recreated. Lean on the 7-day hidden-version window (`daysFromHidingToDeleting: 7` on the bucket)
if something looks wrong mid-restore.

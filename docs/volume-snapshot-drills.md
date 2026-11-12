# volume-snapshot: the revert runbook and the drill record

This page holds two things. The first is the revert runbook for a flux-owned snapshot role,
which the operator follows when `promote` holds a failed revision without resetting `prod`.
The second is the measurements behind the `<tag>-snapshot` Job. The operating rules are in
`ansible/roles/k8s/volume-snapshot/CLAUDE.md`, which is the file a session loads before touching
the role. The measurements are kept out of that file so the rule stays
short and the evidence stays findable (the same split `docs/monitor-bridge-checks.md` makes for
monitor-bridge, #2699).

A text test reads the runbook:
`ansible/tests/longhorn/test_volume_revert_runbook_text.py`. Nothing tests the measurements.
`scripts/renderer/job_files/flux_snapshot.py` is the authority on what the Job does; where a
paragraph here disagrees with it, the script is right.

## The revert runbook

Every step that writes to the cluster or to `DEPLOY_HOLD` is **operator-only**. The agent's
permissions deny `sudo` and every `kubectl` write, and plain `kubectl` is read-only. The
design and the reasoning behind each step are in
`docs/target-architecture/deploy/d5-bulk-move.md`, *The revert runbook (D5.2c)*.

The runbook starts from a stateful hold. When a failed revision R changed a snapshot role,
`promote` writes `DEPLOY_HOLD` as `<R> stateful=<names>`, does **not** reset `prod`, and
posts to the deployer's Discord channel. The post lists every changed tag and every
Kustomization that failed or timed out, each marked as a snapshot role or not. For each
changed snapshot role it prints that role's `digest8`, the snapshot prefix of each claim,
and a command block in Branch 1's order: the classify command, two suspends, one revert run,
and two resumes. Beside the classify command it prints the string a role that attempted R
reads there, `prod@sha256:<digest>`.

**Prerequisites that land in other slices.** The hold post comes from D5.2b1, and the
`reset_to_last_good` input that Branch 1's reset dispatches comes from D5.2b2. Until both
are on master, no stateful hold exists, and the runbook has nothing to start from.

### Choosing the branch

Two reads come first, both with the read-only `kubectl`, and both before any reset. A reset
prunes R's soak Job together with its pod, and the first read needs that pod.

- **Which services the soak named**, when `soak` failed. The soak Job writes them to its
  termination message. The post names the Job and prints the read:
  `kubectl -n flux-system get pods -l job-name=<soak Job> -o jsonpath='{.items[*].status.containerStatuses[0].state.terminated.message}'`.
- **Which changed snapshot roles attempted R.** For each one, run the post's classify
  command, `kubectl -n flux-system get kustomization <s> -o jsonpath='{.status.lastAttemptedRevision}'`,
  and compare what it prints with the value the post prints beside it,
  `prod@sha256:<digest of sha-<R>>`. Equal means the role attempted R. Any other value means
  it did not. The field holds a tag and a digest, never a commit, so a comparison with R's
  SHA never matches. That mistake reads every role as blocked and sends a failed snapshot
  role to Branch 2.
    - Cross-check the comparison value. While the hold keeps `prod` at R,
      `kubectl -n flux-system get ocirepository homelab -o jsonpath='{.status.artifact.revision}'`
      prints the same `prod@sha256:` value. This read is valid only before any reset.
    - A role that an upstream failure blocked never reads R's value there, so its data is
      unchanged. It took no snapshot for R, so `volume-revert` would fail on it with "Fail
      when no snapshot matches this deploy". Leave it out of Branch 1.

Then pick the branch:

- **Branch 1** when a changed snapshot role that attempted R failed: its `<s>` or
  `<s>-verify` failed or timed out, or the soak named its service.
- **Branch 3** when a `<s>-snapshot` Job failed on a full cap.
- **Branch 2** otherwise.

### Branch 1: a snapshot role failed

A reset moves `prod` for the whole bundle, so it rolls back every changed snapshot role, not
only the failed one. So revert every changed snapshot role that attempted R, each from its
own block in the post. A role that did not attempt R needs no step, because the reset leaves
it on the spec it already runs. Run the steps in this order. Resuming comes last, because a
resumed `<s>` re-applies whatever `prod` names.

1. **Suspend every role you revert, before any revert.** For each role, suspend
   `<s>-snapshot`, then `<s>`:

    ```bash
    sudo k3s kubectl -n flux-system patch kustomization <name> --type merge \
      --field-manager=homelab-operator -p '{"spec":{"suspend":true}}'
    ```

   The field manager keeps each suspend in place through every reconcile of the
   Kustomization that applies the suspended object: the root, and from the flux PR on,
   `flux-deferred`. The root therefore needs no suspend of its own.
2. **Revert the volumes, one run per role.** Run the revert line of each role's block in the
   post, exactly as printed. Each line carries that role's own `digest8` and claims, and the
   script takes one service per run. Do not assemble the line by hand from another role's
   block. *The entry point* below describes the script. It leaves each Deployment at zero
   replicas.
3. **Reset `prod`.** Dispatch the reset input:
   `gh workflow run promote.yml -R DanielH2018/server --ref master -f reset_to_last_good=true`.
   The stateful hold names `prod`'s commit, so the reset's in-flight check passes. Then wait
   until the `OCIRepository` `homelab` reports `last-good`'s commit. This read must print
   `master@sha1:<last-good SHA>`:
   `kubectl -n flux-system get ocirepository homelab -o jsonpath='{.status.artifact.metadata.org\.opencontainers\.image\.revision}'`.
   The `revision` field of the artifact holds a digest, not a commit, so it cannot answer
   this wait. The held revision's soak Job then fails by itself within one poll (15s),
   because `homelab` no longer holds its commit. Before that fix a reset left the Job
   running to its deadline, and deleting its pod was the only way to end it (#5704).
4. **Resume each reverted role, in the post's order.** Resume `<s>-snapshot`, then `<s>`:

    ```bash
    sudo k3s kubectl -n flux-system patch kustomization <name> --type merge \
      --field-manager=homelab-operator -p '{"spec":{"suspend":false}}'
    ```

   The snapshot Job for `last-good`'s digest runs again with a fresh token. Then `<s>`
   applies `last-good`'s spec, which restores the replica count.
5. **Clear the hold.** Land the revert of the failed commit. Then clear the hold with
   `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and dispatch `promote`
   for the revert, by D2's procedure in `docs/flux-deploy-plane.md`.

A co-changed role that attempted R and applied it successfully may skip steps 1, 2 and 4
only when the operator records in the hold's thread that its R-written data suits
`last-good`'s image. The reset then rolls it back without a revert. Without that record,
revert it.

### Branch 2: no snapshot role that attempted R failed

The failures are in roles that are not snapshot roles, or in a `<s>-snapshot` Job that
failed for a reason other than a full cap before `<s>` attempted R. No volume needs a
revert, so skip Branch 1's steps 1 to 4. The default is a forward revert: land the revert of
the commit that broke the failing tag, then clear the hold with
`gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and dispatch `promote`. The
changed snapshot roles stay at R, which they applied successfully. Reset `prod` instead only
after confirming that each changed snapshot role's data still suits `last-good`'s image.

### Branch 3: a snapshot Job failed on a full cap

The role never applied R, so its data is unchanged. The Job's failure names the cap, the
space in use and the headroom. `scripts/renderer/job_files/flux_snapshot.py:check_cap` holds
the check. Delete that volume's older snapshots by hand. Then delete
the failed Job so that Flux creates it again. Both are `kubectl` writes. Then clear the hold
with `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server` and dispatch `promote`
for R.

### A verify Job that failed for an environmental reason

A `<tag>-verify` Job can fail while R itself is sound. On 2026-11-12, `crowdsec-verify`
failed because Prometheus could not scrape daniel-pi, which was out of memory and rebooting
(#8060). Its log line names the cause: `crowdsec_verify.py` names the evidence channel and
this section. Nothing applied R wrongly, so no volume needs a revert and no commit needs a
forward revert.

**Clearing the hold alone re-holds.** The Job's name derives from its inputs, so the next
promotion applies the same name. Flux never re-runs a Job that already failed under that
name, so the verify fails again with no new run. Delete the Job first. Every step that
writes is **operator-only**.

1. **Read the failed Job's log** to confirm the cause was environmental:
   `kubectl -n homelab logs job/<the failed Job>`.
2. **Wait until the cause has settled.** For `crowdsec-verify`, `up{job="alloy-pi"}` reads 1
   again in Prometheus.
3. **Delete the failed Job**, so that Flux creates it again:

    ```bash
    sudo k3s kubectl -n homelab delete job <the failed Job>
    ```

4. **Clear the hold** with `gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`,
   and dispatch `promote` for R.

### The entry point

`scripts/backup/volume_revert.py` runs on daniel-box and needs no Ansible. It takes
`--service`, one `--claim` per PVC, `--sha` (the role's `digest8`) and `--namespace`. The hold
post prints one line per role and namespace in this shape:

```bash
sudo .venv/bin/python -B scripts/backup/volume_revert.py --service <s> --namespace <ns> \
  --sha <digest8> --claim <claim> --apply
```

- **It reads the `<tag>-snapshot` Job's snapshots unchanged.** The Job names each one
  `autodeploy-<tag>-<digest8>-<claim>-<token>`, the shape `volume-snapshot` uses. The script
  builds its prefix with `scripts/renderer/renderer_lib/snapshot.py:snapshot_name`, and
  `--sha` takes eight or more lowercase hex digits verbatim.
- **Its dry run answers whether a snapshot exists.** Drop `--apply` and run it as yourself with
  `uv run python`. It resolves each claim and prints the snapshot a real run would revert to.
- **It finds every claim's snapshot before it scales anything**, so a missing snapshot on a
  second claim leaves the service up. A failure after the scale-down leaves the service at zero
  replicas, and `docs/volume-revert-drill-and-sizing.md` has the hand recovery.
- **Nothing runs it unattended.** No cron, unit or workflow calls it.
- **The 2026-11-08 drill passed through the script**, so H7.55 deleted the
  `volume-revert.yml` playbook that had been its fallback.

### The drill

In Wave B, once speedtest is flux-owned, a one-line PR points speedtest's `readinessProbe` at
a missing path. `verify` fails, and `promote` holds without resetting and posts a message
that names speedtest's snapshot. The operator runs this runbook end to end, and then the
revert lands. This is the live proof of the hold rule, the post, the entry point and the
resume order. After the flux PR the drill runs again, and waits through one reconcile of the
root and of `speedtest` before step 2.

### Drill record

The full evidence for each drill is in `docs/target-architecture/deploy/d5-bulk-move.md`.

- **Wave B, before the flux PR.** [#5605](https://github.com/DanielH2018/server/pull/5605)
  broke speedtest's readinessProbe, and the revert
  [#5664](https://github.com/DanielH2018/server/pull/5664) promoted.
- **2026-10-23, after the flux PR (D5 exit 6).**
  [#5998](https://github.com/DanielH2018/server/pull/5998) broke the probe, and Branch 1
  reverted it. Both hand suspends survived a reconcile of `homelab` and `flux-deferred` and
  kept the field manager `homelab-operator`. The held soak Job failed on its own after the
  reset, with no pod delete. speedtest read 1/1 at 15:09:58Z, and the revert
  [#6008](https://github.com/DanielH2018/server/pull/6008) cleared the hold at about 15:14Z.
- **2026-11-08, through `volume_revert.py` (#7509).**
  [#7637](https://github.com/DanielH2018/server/pull/7637) broke the probe, and `promote`
  wrote `2a2090cc… stateful=speedtest`. Both classify reads printed `prod@sha256:939b8243…`.
  The dry run named `autodeploy-speedtest-7f4407a7-config-20261108142446`, and
  `--apply` reverted `speedtest-config` to it and left the Deployment at 0/0. The reset moved
  `homelab` to last-good `1af64196`. After the resumes the snapshot Job ran again, and
  speedtest read 1/1 at about 14:45Z with `probe.py health speedtest` ok. The revert
  [#7641](https://github.com/DanielH2018/server/pull/7641) merged, and the operator cleared
  the hold at about 15:15Z.

## The maintenance-mode attach was never reached by a deploy, and #2740 retired it

Between 2026-08 and 2026-09-27 a claim whose readiness wait timed out on a non-`attached` volume
was attached in maintenance mode, given a second snapshot attempt, then detached. #2740 removed
that block, because two drills measured it unreachable on Longhorn v1.12.1 for both ways a
volume reaches the role detached. The measurements stay as the evidence for the removal.

- **A volume that had been attached before (task-6 drill, 2026-08-21, `speedtest-config`).**
  Invoked against a genuinely detached volume, the ordinary apply and wait path succeeded in
  10.8s, and `volume_snapshot_detached` came out `false`. The Snapshot CR was a real recovery
  point (`size=10436608`, `children={"volume-head":true}`) and survived the re-attach still
  `readyToUse`. The seed pod of `k8s/volume-claim` had also attached the volume before this
  role looked. That cause ended on 2026-09-01, when volume-claim stopped seeding.
- **A volume never attached (#2698 drill, 2026-09-27).** A scratch 1Gi `longhorn-nobackup`
  PVC, `drill-never-attached`, was never mounted by any pod. It read `state=detached` with
  empty `currentNodeID` and `lastAttachedBy`, and its engine was `stopped`. The ordinary path
  returned `readyToUse=true` 13 s after the PVC was created, with `size=0`, and skipped the
  maintenance-mode attach. Longhorn then released its own attachment ticket.
- **Why Longhorn snapshots a detached volume.** In `longhorn-manager` v1.12.1, a new Snapshot
  CR with `createSnapshot: true` makes the snapshot controller call
  `handleAttachmentTicketCreation` before it checks the engine. Longhorn attaches the volume
  itself, takes the snapshot, and deletes its ticket. The code draws no line between a volume
  attached before and one never attached.
- **Why the premise existed.** It entered the tree in `410751a` (2026-08-21), generalised from
  two adjacent operations: the orphan reaper refusing a detached volume, and a revert that got
  a `500`. Neither is snapshot creation. Longhorn has been pinned at v1.12.1 since `ceb8723`
  (2026-08-15), so no older version existed for the premise to be true on.
- **A cleanup trap.** The snapshot of a never-attached volume cannot be deleted until the
  volume attaches. The delete waits on `snapshot deletion delayed: volume engine
  <volume>-e-0 is upgrading from image  to docker.io/longhornio/longhorn-engine:v1.12.1`,
  where the blank image means the engine never ran. Deleting the PVC removes the snapshot CR
  with it. A real first deploy is unaffected, because the pod attaches the volume within the
  run.

A detached claim takes the Job's ordinary path, and a snapshot that never reads ready fails the
Job whatever the volume's state.

## Things measured rather than assumed

- The prune treats anything other than a truthy `status.markRemoved` as not-removed. An absent
  or unpopulated field (a snapshot read moments after creation) counts as live. Every live
  Snapshot CR measured on 2026-08-21 carried the field explicitly (28 `false` / 17 `true`).

## What is unverified

The create path and the prune have both run on real deploys: most claims sit at exactly
`volume_snapshot_retain` (3) Snapshot CRs. To count them and see which are ready, list the
`autodeploy-*` CRs:

```bash
kubectl -n longhorn-system get snapshots.longhorn.io \
  -o custom-columns=NAME:.metadata.name,READY:.status.readyToUse --no-headers | grep '^autodeploy-'
```

The only CRs that ever stayed unready were 13 over-long ones from 2026-08-22, which no
supported route could delete (#2686). The operator removed them on 2026-09-27 by bypassing
Longhorn's webhook for exactly those objects (#2734). *The 13 over-long CRs the prune retries and Longhorn refuses, removed by hand
2026-09-27* below has the source reading and the method. Still unverified:

- **`readyToUse` timing** against the 120s ceiling has not been measured.

## The Job's opt-in and retention

### Which roles opt in

The set of roles that opt in is the table below. It is generated from each role's
`defaults/main.yml`, so it moves when a role declares or drops `k8s_autodeploy_snapshot_pvcs`.
A role that only mentions the name in a comment, as `observability` and `navidrome` do, is not
in it.

--8<-- "assets/generated/fragments/snapshot-optin.md"

The opt-in set follows the auto-deploy promotion criteria, not a data-migration survey. The
roles with the `Recreate` + rendered-RWO-claim shape that did not opt in carry the same
migration risk. Widening was deferred until the create path had run live, and it has since; see
*What is unverified* above.

### The revert needs two, not one

`scripts/renderer/job_files/flux_snapshot.py:RETAIN_FLOOR` holds a floor of 2, whatever
`volume_snapshot_retain` says. A rollback takes its own snapshot and prunes before the revert
reads the chain. At that prune, the chain holds two snapshots that both matter: the one just
taken, and the earlier snapshot the revert needs. A floor of 1 keeps only the newest and deletes
the recovery point.

**The retention window holds the 3 most recent Job runs, not the 3 most recent distinct
revisions.** The per-run token gives every run its own snapshot, so three runs at one role
`digest8` fill the window and prune whatever came before them. Without the token, a second run
at one digest would name the same CR as the first, which could be `markRemoved`-but-not-gone.
`create` never adopts such a CR. The token fixes that collision, and a window counted in runs is
its cost.

**Known cost, not fixed here: renaming a service strands its old snapshots permanently.** The
prune selects on `autodeploy-<tag>-`, and `scripts/backup/longhorn_reap.py snapshots` — the
cluster-wide orphan reaper — skips any snapshot carrying no `RecurringJob` label, which every
`autodeploy-*` snapshot does by construction. Rename a service and its snapshots under the old
name become invisible to both the Job's prefix filter and the reaper: nothing prunes them,
nothing reaps them, and they pin their blocks against `filesystem trim` forever.

### The 13 over-long CRs the prune retries and Longhorn refuses, removed by hand 2026-09-27

**They are gone.** The operator removed all thirteen on 2026-09-27 (#2734), bypassing the webhook
for exactly those objects; *How they were removed* below has the method. The rest of this section
records why they could not be deleted through any supported route, which holds for any over-long
name.

Thirteen `autodeploy-*` Snapshot CRs created on 2026-08-22 across four volumes
(`code-server-config`, `code-server-workspace`, `home-assistant-config`, `qbittorrent-config`)
could not be deleted through the Kubernetes API at all. On 2026-09-26 they read
`status.readyToUse: false`, most carrying `status.error` "lost track of the corresponding
snapshot info inside volume engine." Issue #2686 filed them as never pruned. The prune did
reach them; the delete was what failed. Eleven were `markRemoved: false` with the lost-track
error. The other two, both `code-server-workspace`, carried `markRemoved: true`.

**The cause is the 63-byte name ceiling, and these CRs predate its fix by hours.** `0c0317a77`
(2026-08-22) dropped the redundant `<service>-` from the claim segment. Before it those four
claims rendered names of 65, 68, 71 and 65 bytes; after it, 53, 56, 56 and 53.

**Teaching the orphan reaper the `autodeploy-` prefix would not have helped.** It skipped them
for the reason the rename bullet above gives, and it deletes through `kubectl` as well, so it
would have hit the same webhook.

**No supported route removes an over-long CR on Longhorn v1.12.1, the manager API included.**
Read from the `longhorn-manager` v1.12.1 source on 2026-09-27:

- Only a Kubernetes DELETE sets the `deletionTimestamp` that the snapshot controller's
  finalizer removal waits on.
- The validator's `Delete` hook calls `IsSnapshotLinkedCloneEntrypoint`, which builds a label
  selector from the raw snapshot name. That selector is invalid past 63 bytes, so the webhook
  denies every DELETE, whatever client sends it.
- The manager API's `snapshotDelete` action (`VolumeManager.DeleteSnapshot`) deletes from the
  engine's chain and never touches the CR. The engine no longer held these snapshots, so it had
  nothing to delete. The UI's per-volume delete makes the same call.
- The manager API's `snapshotCRDelete` action goes through `DeleteSnapshotCR`, which is a
  Kubernetes DELETE and hits the same webhook.
- The controller sets the lost-track error and nothing else. It never removes such a CR itself.

Upstream master still builds the selector from the raw name, so an upgrade does not fix this
either. `scripts/renderer/job_files/flux_snapshot.py:NAME_LIMIT` keeps new snapshots under the
ceiling.

**They pinned no blocks.** Measured 2026-09-27, before the removal: none of the thirteen names
appeared in `status.snapshots` of any of the four volumes' running engines. The controller sets
the lost-track error exactly when a CR's name is missing from that map. One `markRemoved` CR
still reported `size: 344064`, which is a stale status field rather than blocks in the chain.

**How they were removed.** An operator-approved scratch play, run from a direct operator session
because the auto-mode classifier refuses a webhook bypass from an agent session:

1. It asserts the targets by name, and asserts that `longhorn-webhook-validator` carries
   `objectSelector: {}`.
2. It labels only the targets, which is an UPDATE the validator allows.
3. It sets that `objectSelector` to skip that label. For a DELETE, the selector matches
   the old object, so every other object in the cluster stays validated.
4. It deletes the targets by exact name, then restores `objectSelector: {}` in an `always` block
   and asserts the restore.

The webhook configuration is applied by longhorn-manager through wrangler's `objectset` with the
`longhorn-webhook-ca` Secret as owner. longhorn-manager re-applies it on start or on a CA change,
not on a short loop, so nothing raced the patch and the restore was the play's job. Two runs, both
`failed=0` with nothing left afterwards: the eleven lost-track CRs, then the two `markRemoved`
ones.

# Longhorn upgrade runbook

Longhorn holds every PVC in the cluster, supports **no downgrade**, and since v1.5.0 supports
**only one minor version per hop**. So an upgrade is a ladder of discrete hops, each with its own
verification, not a single version bump.

The install path is Flux. The `longhorn` role vendors upstream's `deploy/longhorn.yaml` at the
pinned version, byte for byte, under `ansible/roles/k8s/longhorn/files/upstream/`, and the
`longhorn` Kustomization applies it. That is the same manifest upstream's supported upgrade path
applies for a minor bump. Everything below therefore goes through the merge of the pin — a hand-run
`kubectl apply` would deploy the same manifest while leaving the repo as a stale source of truth.

## The gate — before any hop

Upstream's own instruction is *"Always back up volumes before upgrading. If anything goes wrong,
you can restore the volume using the backup."* With no downgrade path, that is the whole safety net.

Each condition is a stop, not a checklist item. They run as one script, in order, and the exit
code names the first gate that refused (#2216, the shape `docs/k3s-upgrade.md` set):

```bash
uv run python scripts/deploy_tools/runbook_gates.py longhorn-upgrade
```

Exit 0 means all four passed. Exit 1–4 is the gate that failed, and the script prints what it
found. Exit 69 means the cluster could not be asked. Run it on daniel-box: gate 2 reads the
restore drill's stamp from its ConfigMap, and while that holds none, from the drill's directory on
daniel-box.

**`land.sh` runs these gates at the merge.** Merging the pin is the upgrade (H1 Open question 6,
ruled 2026-10-16), so `land.sh --arm-merge --await-merge` runs
`runbook_gates.py longhorn-upgrade` before it merges a PR that changes `k3s_longhorn_version`.
`scripts/deploy_tools/land_lib/pin_gate.py` owns the check. The pin is `k3s_longhorn_version` in
`ansible/roles/k8s/longhorn/defaults/main.yml`. Before the gates, it refuses in three cases:

- on any host but daniel-box;
- while `DEPLOY_HOLD` reads anything but `none`, empty and missing included;
- while (`last-good`, master tip] already holds a change to the pin, so a second bump never
  merges ahead of the first one's verification.

On a pass it posts the commit status `homelab/upgrade-gates` on the PR's head SHA. `promote`
requires that status, at most six hours old, before it applies the bump. A merge made any other way
skips the gate. For such a merge, `land.sh --regate <pr>` re-runs the gates and re-posts the status
without merging. A fix-forward bump after a failed bump is **operator-only**: the operator
merges it outside `land.sh`, runs `land.sh --regate <pr>` for it, then clears the hold with
`gh variable set DEPLOY_HOLD --body none -R DanielH2018/server`.

1. **The backup target is armed and available.** `default` must carry a URL, and every target
   that carries one must report `available` — an armed target that is not available cannot
   list what it holds, let alone restore it.
2. **A restore has succeeded recently.** A green backup job is not proof — on 2026-08-15 a B2
   Class-B cap denial surfaced as `cannot find volume.cfg in backupstore`, which reads as data
   loss. The nightly drill's `last-success` stamp must be younger than
   `k3s_longhorn_restore_drill_max_age_days`, the same window backup health's check 7 pages
   on. `docs/longhorn-disaster-recovery.md` names the two places the stamp lives. A stale stamp means: fix the drill first, or run it by hand
   (an **operator-only** Job from the CronJob: `kubectl -n homelab create job
   --from=cronjob/longhorn-restore-drill longhorn-restore-drill-manual-1`, outside the 04:10 UTC
   slot; `docs/k3s-node-plane-crons-and-incidents.md` has the pinned form).
3. **Every volume accounted for.** Attached or detached, and healthy (an idle volume reports
   `unknown`). A volume mid-attach, `degraded` or `faulted` before the hop cannot be told apart
   from upgrade damage after it.
4. **Every engine on one image.** The previous hop's engine image must hold zero references
   before the next hop starts — see step 4 of the procedure below for why.

## Per-hop procedure

1. **Bump and refresh.** Set `k3s_longhorn_version` in `ansible/roles/k8s/longhorn/defaults/main.yml`
   to the latest patch of the *next* minor. Never skip a minor. Then, in the same PR, refresh the
   vendored copy:
   ```bash
   uv run python scripts/dev/vendor_manifest.py longhorn
   ```
   The command fetches the file at the new version, writes it under
   `ansible/roles/k8s/longhorn/files/upstream/`, rewrites the pin's `fetched_url` and `sha256`, and
   prints each object the old file declared and the new one lacks. The tag renders `prune: false`,
   so an object that upstream removed stays live. Name each one in the PR body and say how it is
   removed (an **operator-only** delete).
2. **Re-sync the StorageClass.** Diff the new version's `longhorn-storageclass` ConfigMap, which sits in
   the vendored `longhorn.yaml`, against
   `ansible/roles/k8s/longhorn/files/patches/longhorn-storageclass.yaml`. Carry over any new
   parameters, and update the provenance in its header. **Never** paste `numberOfReplicas` back in —
   `default-replica-count` is the single source of truth.
   `ansible/tests/longhorn/test_longhorn_storageclass.py` fails if the parameter reappears, and
   fails if the patched class differs from upstream's in anything but that parameter. Apply the same
   diff to `ansible/roles/k8s/longhorn/templates/storageclass-nobackup.yaml.j2`.
3. **Merge through `land.sh`.** The merge is the apply, in place of `k3s-bringup.yml --tags longhorn`.
   On daniel-box, run the command the `land-after-merge` skill gives for this PR. `land.sh` runs
   the gates above, posts `homelab/upgrade-gates`, merges, and follows the merge until `promote`
   has applied it. The `longhorn` Kustomization renders `wait: true`, so Flux waits for every
   object it applies, the manager DaemonSet and the driver deployer among them, within the
   `manifests_rollout_timeout` in the role's defaults. Then confirm the driver deployer, which
   the old play waited on:
   ```bash
   kubectl -n longhorn-system rollout status deploy/longhorn-driver-deployer --timeout=600s
   ```
   A new StorageClass parameter changes a field that Kubernetes treats as immutable. The manager
   does not necessarily replace a class whose ConfigMap changed. Before the next step, run the
   health gate on daniel-box:
   ```bash
   uv run python scripts/diagnostics/probe.py health longhorn
   ```
   After a healthy rollout, the gate compares the live `longhorn` class's parameters with the
   class in ConfigMap `longhorn-system/longhorn-storageclass`
   (`scripts/diagnostics/probe_lib/health_plumbing.py:longhorn_storageclass`). A match prints
   `StorageClass longhorn matches`. A mismatch exits 1 with the head line `longhorn: the
   plumbing check FAILED the gate after a healthy rollout` and a `FAILED:` line that names
   each parameter whose live and ConfigMap values differ. An exit 1 without that head line
   comes from the rollout or the alert plane, and the comparison may not have run. Exit 75
   means a read failed, which is no verdict. When the parameters differ, the fix is an **operator-only** delete of the class, which the
   manager recreates from the ConfigMap. Deleting a class is safe, because parameters are read
   at provision time only.
4. **Wait for the engines to follow the manager.** Upstream ships
   `concurrent-automatic-engine-upgrade-per-node-limit` at `0`, which leaves engines on the old
   image. This repo sets it to `3` (`k3s_longhorn_concurrent_engine_upgrade` in the `longhorn`
   role's defaults), so Longhorn upgrades the engines of healthy volumes by itself, three per
   node at a time. A read-only `kubectl -n longhorn-system get settings.longhorn.io
   concurrent-automatic-engine-upgrade-per-node-limit -o jsonpath='{.value}'` printed `3` on
   2026-10-22, and `ansible/tests/longhorn/test_engine_upgrade_limit_is_positive.py` fails a
   render that sets it to `0`. Wait until the previous engine image holds zero references
   before starting the next hop, because otherwise engine lag compounds across hops:
   ```bash
   kubectl -n longhorn-system get engineimages.longhorn.io
   ```
   Longhorn skips a degraded volume, so a volume that stays on the old image needs its own
   look before the next hop.
5. **Verify** before declaring the hop done: every volume healthy, the StorageClass still carries no
   `numberOfReplicas`, and `default-replica-count` still reads its expected value.

## Lessons from the ladder

The ladder from v1.7.2 to v1.12.1 is walked, and v1.12.1 to v1.13.0 is the first hop taken
through Flux (H1.8b). `k3s_longhorn_version` records where it stands. Four lessons carry to the next hop:

- **StorageClass parameters are immutable.** Kubernetes forbids updating `parameters` on an
  existing StorageClass, so an apply of the class itself fails when a release adds one (v1.8
  added `backupTargetName`). Until H1.5a, a task in `roles/setup/k3s/tasks/longhorn.yml` deleted
  the class when the whole live parameter map differed from the rendered class. That task is gone,
  and the manager creates the class from the ConfigMap. Step 3 above says what to do when the live
  class differs. Deleting is safe, because parameters are read at provision time only.
- **The manager manifest and the Settings apply in the same revision.** A removed or renamed
  setting fails to apply after the new version is live (v1.8 moved backup-target configuration
  from settings to `backuptargets.longhorn.io`). Remove or rename the Setting in the `longhorn`
  role's `templates/settings.yaml.j2` and land that, because Flux owns the object and reverts a
  hand edit.
- **Check the backup targets after a hop, not only the manager.** The `longhorn-backup`
  Kustomization owns the targets and the RecurringJobs. `probe.py health longhorn-backup` reads
  each target's `status.available`.
- **Read the release notes for image and Kubernetes floors.** v1.10 fully qualified image
  refs, so every node re-pulls and shows a transient `ImagePullBackOff`. v1.11 requires
  Kubernetes ≥ v1.34, and v1.13 keeps that floor for its CSI external-provisioner v6.3.0.
  v1.13 also adds the `longhorn-global-manager` Deployment, three replicas with a preferred
  (not required) anti-affinity. The role's `longhorn-global-manager.yaml` patch cuts it to two,
  one per node, and gives it a memory request (#5968).

- **The manager DaemonSet rolls one pod at a time** (#6009). Upstream ships
  `maxUnavailable: 100%`, and the role's `longhorn-manager-update-strategy.yaml` patch sets
  `1`. The manager pods serve the admission webhook that every claim's dry run calls. On
  the v1.13.0 hop both pods went down at once, and four claim-holding tags posted `failure`
  for two minutes. The patch header has the evidence that 100% is not needed for an upgrade.
  A new manager turns Ready before it waits for the old pods to go, so a hop cannot stall
  with one pod down. The cost is that a hop runs the old and new webhooks side by side for
  one pod's start.

### Expected-red node conditions

Two node-CR conditions read `False` here and neither is an upgrade failure:

- **`Multipathd=False`** — long-standing. `multipathd` runs, and the role blacklists Longhorn
  devices from it (`/etc/multipath.conf`). Longhorn flags the daemon's presence regardless.
- **`KernelModulesLoaded=False`** — a node condition added in v1.10. It reports `dm_crypt`
  missing, which is only required for **encrypted** volumes. Nothing here uses volume
  encryption, so this is informational. It would become real if an encrypted StorageClass were
  ever added.

Check `Ready` for the actual health signal; the other conditions are advisory.

## Staying current afterwards

Renovate tracks the pin (`renovate.json`, the annotation manager for
`ansible/roles/k8s/longhorn/defaults/main.yml`) with `automerge: false`, in a `Longhorn (manual — …)`
group of its own that names the merge gate above. Land its PR through `land.sh`. A Renovate PR that jumps
more than one minor is not directly mergeable, because Longhorn supports one minor per hop.
Treat that PR as the signal to take the next minor through the per-hop procedure above. A PR
for the next minor or a patch is directly actionable through that procedure, after its manual half:
the refresh in step 1 and the class re-sync in step 2, which Renovate cannot do.

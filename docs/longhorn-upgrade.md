# Longhorn upgrade runbook

Longhorn holds every PVC in the cluster, supports **no downgrade**, and since v1.5.0 supports
**only one minor version per hop**. So an upgrade is a ladder of discrete hops, each with its own
verification, not a single version bump.

The install path is `kubectl apply -f .../<version>/deploy/longhorn.yaml`
(`roles/setup/k3s/tasks/longhorn.yml`, task *Install Longhorn*), which is upstream's supported upgrade
path for a minor bump. Everything below therefore goes through Ansible — a hand-run `kubectl apply`
would deploy the same manifest while leaving the repo as a stale source of truth.

## The gate — before any hop

Upstream's own instruction is *"Always back up volumes before upgrading. If anything goes wrong,
you can restore the volume using the backup."* With no downgrade path, that is the whole safety net.

Each condition is a stop, not a checklist item. They run as one script, in order, and the exit
code names the first gate that refused (#2216, the shape `docs/k3s-upgrade.md` set):

```bash
uv run python scripts/deploy_tools/longhorn_upgrade_gates.py
```

Exit 0 means all four passed. Exit 1–4 is the gate that failed, and the script prints what it
found. Exit 69 means the cluster could not be asked. Run it on daniel-box: gate 2 reads the
restore drill's stamp, which exists only there.

1. **The backup target is armed and available.** `default` must carry a URL, and every target
   that carries one must report `available` — an armed target that is not available cannot
   list what it holds, let alone restore it.
2. **A restore has succeeded recently.** A green backup job is not proof — on 2026-08-15 a B2
   Class-B cap denial surfaced as `cannot find volume.cfg in backupstore`, which reads as data
   loss. The nightly drill's `last-success` stamp must be younger than
   `k3s_longhorn_restore_drill_max_age_days`, the same window monitor-bridge's check 7 pages
   on. A stale stamp means: fix the drill first, or run it by hand
   (`sudo /usr/local/bin/longhorn-restore-drill.sh`).
3. **Every volume accounted for.** Attached or detached, and healthy (an idle volume reports
   `unknown`). A volume mid-attach, `degraded` or `faulted` before the hop cannot be told apart
   from upgrade damage after it.
4. **Every engine on one image.** The previous hop's engine image must hold zero references
   before the next hop starts — see step 4 of the procedure below for why.

## Per-hop procedure

1. **Bump** `k3s_longhorn_version` in `ansible/roles/setup/k3s/defaults/main.yml` to the latest
   patch of the *next* minor. Never skip a minor.
2. **Re-sync the StorageClass.** Diff the new version's `storageclass.yaml` block inside
   `deploy/longhorn.yaml` against `roles/setup/k3s/files/longhorn-storageclass.yaml`, carry over any
   new parameters, and update the provenance line in its header. **Never** paste
   `numberOfReplicas` back in — `default-replica-count` is the single source of truth, and
   `ansible/tests/longhorn/test_longhorn_storageclass.py` fails if the parameter reappears.
   Apply the same diff to `longhorn-storageclass-nobackup.yaml`.
3. **Deploy**, on daniel-box (the play refuses to run anywhere else):
   ```bash
   uv run ansible-playbook ansible/k3s-bringup.yml --tags longhorn
   ```
   The role already waits on the `longhorn-driver-deployer` rollout.
4. **Upgrade the engines.** `concurrent-automatic-engine-upgrade-per-node-limit` is `0`, so engines
   do *not* follow the manager automatically. Upgrade them, then confirm the previous engine image
   has dropped to zero references before starting the next hop — otherwise engine lag compounds
   across hops:
   ```bash
   kubectl -n longhorn-system get engineimages.longhorn.io
   ```
5. **Verify** before declaring the hop done: every volume healthy, the StorageClass still carries no
   `numberOfReplicas`, and `default-replica-count` still reads its expected value.

## Lessons from the ladder

The ladder from v1.7.2 to v1.12.1 is walked, and `k3s_longhorn_version` records where it
stands. Four lessons carry to the next hop:

- **StorageClass parameters are immutable.** Kubernetes forbids updating `parameters` on an
  existing StorageClass, so `kubectl apply` fails when a release adds one (v1.8 added
  `backupTargetName`). The task *Delete the StorageClass while its parameters differ…* in
  `roles/setup/k3s/tasks/longhorn.yml` therefore compares the whole live parameter map against
  the rendered class. Deleting is safe, because parameters are read at provision time only.
- **The manager manifest applies before the role's settings patches run.** A removed or
  renamed setting fails after the new version is live (v1.8 moved backup-target configuration
  from settings to `backuptargets.longhorn.io`). Fix the task and re-run.
- **Run every tag the role owns after a hop, not only the one you upgraded under.** Tasks
  tagged `longhorn_backup` alone were not exercised by `--tags longhorn`.
- **Read the release notes for image and Kubernetes floors.** v1.10 fully qualified image
  refs, so every node re-pulls and shows a transient `ImagePullBackOff`. v1.11 requires
  Kubernetes ≥ v1.34.

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

Renovate tracks the pin (`renovate.json`, manager for
`ansible/roles/setup/k3s/defaults/main.yml`) with `automerge: false`. A Renovate PR that jumps
more than one minor is not directly mergeable, because Longhorn supports one minor per hop.
Treat that PR as the signal to take the next minor through the per-hop procedure above. A PR
for the next minor or a patch is directly actionable through that procedure.

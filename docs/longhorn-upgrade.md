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

```bash
# 1. The backup target must be armed and reachable
kubectl -n longhorn-system get backuptarget default \
  -o jsonpath='{.spec.backupTargetURL}{"  avail="}{.status.available}'

# 2. A restore must actually succeed. A green backup job is not proof — on 2026-08-15 a B2
#    Class-B cap denial surfaced as "cannot find volume.cfg in backupstore", which reads as
#    data loss. Run the drill.

# 3. Every volume accounted for: no volume in an unknown state, or you cannot tell
#    upgrade damage from what was already broken.
kubectl -n longhorn-system get volumes.longhorn.io \
  -o custom-columns='NAME:.metadata.name,STATE:.status.state,ROBUST:.status.robustness'
```

## Per-hop procedure

1. **Bump** `k3s_longhorn_version` in `ansible/roles/setup/k3s/defaults/main.yml` to the latest
   patch of the *next* minor. Never skip a minor.
2. **Re-sync the StorageClass.** Diff the new version's `storageclass.yaml` block inside
   `deploy/longhorn.yaml` against `roles/setup/k3s/files/longhorn-storageclass.yaml`, carry over any
   new parameters, and update the provenance line in its header. **Never** paste
   `numberOfReplicas` back in — `default-replica-count` is the single source of truth, and
   `ansible/tests/test_longhorn_storageclass.py` fails if the parameter reappears.
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

## Hop-specific notes

| Hop | Target | Note |
|---|---|---|
| 0 | v1.7.3 | Patch, same minor. StorageClass is identical to v1.7.2's. A rehearsal of the loop at the lowest possible risk. |
| 1 | v1.8.2 | **See the landmine below.** Multiple backup targets arrive here; the StorageClass gains `backupTargetName: "default"`. |
| 2 | v1.9.2 | No deprecations noted upstream. |
| 3 | v1.10.2 | Image refs become fully qualified (`docker.io/longhornio/...`), so every node re-pulls rather than reusing its cached copy — expect a transient `ImagePullBackOff` and `Ready=False` while that happens. Adds a `KernelModulesLoaded` node condition; see below. |
| 4 | v1.11.3 | Requires **Kubernetes ≥ v1.34** (CSI external-provisioner v6.3.0). Confirm CSI pods land before declaring done. |
| 5 | v1.12.1 | Deprecates legacy **V2** linked-clone volumes. Does not apply while every volume is `dataEngine: v1` — re-check before the hop. `default-replica-count` becomes a per-data-engine map (`{"v1":"2","v2":"2"}`); the role still patches a scalar `"2"` and Longhorn normalises it, so the task stays idempotent. |

### Landmine at hop 1: StorageClass parameters are immutable

v1.8.2 adds `backupTargetName: "default"` to the default StorageClass. Kubernetes forbids updating
`parameters` on an existing StorageClass, so `kubectl apply` **cannot** make this change — it fails
rather than silently diverging.

The role already handles one instance of this (*Delete the StorageClass while it still pins a
replica count*), but that guard only fires when `numberOfReplicas` is present:

```yaml
when: k3s_longhorn_sc_replicas.stdout | default('', true) | length > 0
```

A `backupTargetName` addition does not match it. The guard now compares the whole live parameter
map against the rendered class and deletes when they differ. Deleting is safe — parameters are read
at provision time only, so it neither touches an existing PV nor unbinds a PVC.

### Second landmine at hop 1: backup-target config moved out of settings

v1.8.0 moved backup-target configuration off the global settings and onto
`backuptargets.longhorn.io`, as part of supporting more than one target. The
`backupstore-poll-interval` **setting no longer exists**, so the role's patch failed with:

```
Error from server (NotFound): settings.longhorn.io "backupstore-poll-interval" not found
```

The task now patches `backuptargets.longhorn.io/default` at `spec.pollInterval`, which takes a
duration string (`"0s"`) where the setting took bare seconds (`0`). The upgrade itself migrates the
existing value across correctly — the CR came out of the hop already holding `0s` — so the risk is
the play aborting mid-hop, not a lost setting.

The general lesson for the remaining hops: **the manager manifest applies before the role's settings
patches run**, so a removed or renamed setting fails *after* the new version is already live. That
is recoverable (fix the task, re-run), but expect it once per hop where upstream reorganises
settings.

**A tag can hide the same breakage.** `backupstore-poll-interval` was caught because its task is
tagged `[longhorn, longhorn_backup]` and the ladder ran `--tags longhorn`. The two tasks that set
the target itself are tagged `longhorn_backup` **only**, so they were never exercised during the
upgrade — and by v1.12.1 the `backup-target` and `backup-target-credential-secret` settings are
*also* gone. They now patch `backuptargets.longhorn.io/default` at `spec.backupTargetURL` and
`spec.credentialSecret`.

The arm/disarm semantics are unchanged and were re-verified against a disarmed cluster: with
`--type=merge`, an empty string is written rather than the field being removed, so
`k3s_longhorn_backup_armed=false` still *enforces* the blank instead of leaving whatever is live.
After the migration a disarmed run reported `changed=0` and the target stayed
`backupTargetURL=""`, `available=false`.

**When a hop finishes, run every tag the role owns, not just the one you upgraded under** —
otherwise a whole code path stays untested until the night you need it.

### Expected-red node conditions

Two node-CR conditions read `False` here and neither is an upgrade failure:

- **`Multipathd=False`** — long-standing. `multipathd` runs, and the role blacklists Longhorn
  devices from it (`/etc/multipath.conf`). Longhorn flags the daemon's presence regardless.
- **`KernelModulesLoaded=False`** — new node condition, first seen at hop 3. It reports
  `dm_crypt` missing, which is only required for **encrypted** volumes. Nothing here uses volume
  encryption, so this is informational. It would become real if an encrypted StorageClass were ever
  added.

Check `Ready` for the actual health signal; the other conditions are advisory.

## Staying current afterwards

Renovate tracks the pin (`renovate.json`, manager for
`ansible/roles/setup/k3s/defaults/main.yml`) with `automerge: false`. While the ladder is being
walked it will offer the newest release — several minors ahead and therefore **not** directly
mergeable. Treat those PRs as a signal to take the next hop, not as the hop itself.

Once current, you are at most one minor behind at any time and the no-skip rule stops binding, so
the PR becomes directly actionable via the per-hop procedure above.

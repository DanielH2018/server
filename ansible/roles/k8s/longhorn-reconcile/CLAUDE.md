# longhorn-reconcile — the hourly job that holds each volume in its backup tier

A CronJob in `longhorn-system` that labels Longhorn Volume CRs with the recurring-job group
their tier names and sets each Volume's backup target. It replaced the label, weekly-shard and
legacy-label tasks that `setup/k3s` ran from `tasks/longhorn.yml` and
`tasks/longhorn-weekly-shard.yml` (H1.6), and the two target-routing tasks of
`tasks/longhorn-backup.yml` (H1.7a).

It does not touch the CSI sidecar Deployments. From H1.6 until #5958 it patched their memory
request, and longhorn-manager cleared it on its own hourly tick, so each of the four
Deployments rolled about twice an hour. The request is the `longhorn` role's
`system-managed-csi-components-resource-limits` Setting, which the manager enforces. Do not
add a Deployment write back here.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn-reconcile` with exit 5
- **Image:** `python` (`longhorn_reconcile_k8s_image`)
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.6). Its `containers_list` entry carries `owner: flux` and
  `depends_on: [longhorn]`, because its objects live in the `longhorn-system` namespace that
  `longhorn`'s vendored file creates. `tasks/main.yml` holds the include of `k8s/manifests` and
  no Flux path runs it. It is a tag of its own, not part of `longhorn`, because its ConfigMap
  renders from `containers_list`: inside `longhorn`, every claim or tier change would change a
  forward-only tag. This tag is not forward-only and prunes by default.
- **`NEVER_SHED` omits the tag and it has no `tier:`.** It renders a CronJob and nothing to
  scale, so `probe.py shed-set` names it on its "nothing to scale" line.
- **Three templates.** `templates/cronjob.yaml.j2` is the job, `templates/configmap.yaml.j2`
  carries the script, `flux_job_connect.py` and `tiers.json`, and `templates/rbac.yaml.j2` is
  the ServiceAccount and its grants.
- **`tiers.json` is the role's three tier lists.** `ansible/roles/k8s/longhorn-reconcile/defaults/main.yml`
  derives `k3s_longhorn_r2_volumes`, `k3s_longhorn_weekly_volumes` and
  `k3s_longhorn_nobackup_volumes` from `containers_list` with `tier_backup_claims`,
  `longhorn_weekly_claims` and `longhorn_nobackup_claims` (H1.7a moved them here from
  `setup/k3s`, under the same names). The ConfigMap renders the three keys, and the docs
  generators, the catalogue and the tier tests resolve the same expressions through
  `scripts/lib/service_tiers.py:resolved_tier_lists`.
  `tests/test_longhorn_reconcile_render.py::test_the_configmap_tiers_are_the_three_lists_of_the_role_defaults`
  holds the two forms equal, and
  `tests/test_longhorn_reconcile_render.py::test_setup_k3s_no_longer_defines_the_tier_lists`
  holds the keys to one home. The script reads `r2` for each Volume's `spec.backupTargetName`
  and `weekly` and `nobackup` for the group labels.
- **Claims are `namespace/pvcName`.** The script reads the PVC a Volume CR serves from
  `status.kubernetesStatus`. A volume that serves none reads as `/`, is on no list, and is
  left alone unless it carries the retired flat `weekly-backup` label.

## What it writes, and how often (the autonomous-role contract)

The job runs hourly (`17 * * * *`, `concurrencyPolicy: Forbid`) and once per role revision as a
gate Job in `longhorn-reconcile-verify`. It changes state with no human in the loop, so its
authority is written here to keep a later edit from widening it.

- **Writes, and nothing else.**
    - A JSON merge patch on a `volumes.longhorn.io` object. It touches only the label keys
      `recurring-job-group.longhorn.io/{default, no-backup, weekly-backup, weekly-backup-d0..d6}`
      and `spec.backupTargetName`. One patch per volume that must move, labels and target
      together.
    - The same patch carries the per-job label `recurring-job.longhorn.io/trim` (H2.2a). The
      script never writes a `recurring-job-group.longhorn.io/trim` key.
- **The trim arm labels an attached volume that a ReplicaSet or StatefulSet runs.** The volume's
  `status.state` is `attached` and `status.kubernetesStatus.workloadsStatus` holds a
  `Running` ReplicaSet or StatefulSet. The arm removes the label when either stops holding, so
  a CronJob-only claim such as `pi-peer-backup-data` never joins and the `trim` RecurringJob
  attaches no idle volume. A volume that gains the label and sits in the daily tier also gets
  `recurring-job-group.longhorn.io/default`, because Longhorn adds `default` only to a volume
  with no job of its own. It never sets `default` on a `no-backup` or weekly-shard volume.
  The rule lags by up to an hour, because the job runs hourly.
- **The trim arm covers every volume the rule admits** (H2.2b). No list bounds it. Every run
  prints `trim would-admit: <claims>`, the claims the rule admits, which are the claims the arm
  labels. The arm calls only `list` and `patch` on volumes, so the grants below are unchanged.
  `tests/test_longhorn_reconcile_trim.py::test_every_volume_the_rule_admits_gets_the_trim_label`
  holds the unbounded set.
- **A converged cluster gets no write.** The run reads the Volume list and the PVC list, plans,
  and patches only what differs. `tests/test_longhorn_reconcile.py::test_a_second_run_changes_nothing`
  proves it, and the gate Job's `homelab/rerun-safe` names that test.
- **The target arm moves two cases and nothing else.** A volume whose claim is on the `r2`
  list and whose target is not `r2` moves to `r2`. A volume on `r2` whose claim is on no list
  returns to `default`, the B2 target. Any other volume keeps its target, a blank one included:
  the Ansible tasks this arm replaced moved only those two cases, and normalising blank to
  `default` would patch every volume on the first run.
  `tests/test_longhorn_reconcile.py::test_each_target_transition_writes_the_live_target_name`
  holds the table, and
  `tests/test_longhorn_reconcile.py::test_the_target_names_are_the_ones_setup_k3s_and_the_readers_use`
  holds the names to `longhorn_lib/longhorn_backups.py`.
- **Report-only first.** `longhorn_reconcile.py --report-only` makes the same two reads and
  prints `would patch ...` lines with no write. Run it with a read-only credential before a
  change to the script or to a tier list; on a converged cluster it prints no `would patch`
  line.
- **RBAC is the verbs the script calls, and the target arm adds none.** `list` and `patch` on
  `volumes.longhorn.io`, and a ClusterRole with `list` on `persistentvolumeclaims`. No grant
  reaches a Deployment. `get` on volumes is absent because the script lists once and never
  reads a single volume. ENFORCED:
  `ansible/tests/k8s/test_rbac_against_rest_script.py::test_the_longhorn_reconcile_role_grants_every_call_its_script_makes`.
- **A failed read writes nothing.** Both lists are read before the first patch. A refused patch
  fails the run (exit 1); the next hourly run retries.
- **A claim on both the weekly and the no-backup list stops the run before any write.** The
  Ansible tasks gave such a volume both labels, which nothing intended.
- **A volume in no group stays in none.** Every step moves a volume out of a group it carries.
  `tests/test_longhorn_reconcile.py` holds that with the `a-volume-in-no-group-is-left-alone`
  case.
- **Scope of the return to `default`.** A `no-backup` volume returns to `default` only when its
  PVC is `longhorn`-class and on neither list. Most no-backup volumes were never on a list:
  `longhorn-nobackup` excluded them at creation, and sweeping them into the daily set is the
  B2 transaction blowup this job exists to prevent.
- **A weekly-listed volume that sits in `no-backup` is not moved.** The Ansible tasks never
  moved it either, and `ansible/tests/longhorn/test_daily_group_membership_is_the_r2_list.py`
  pins that exclusion. Remove the volume from the no-backup class by hand first.

## Traps

- **A second writer to the CSI sidecars' `resources` restarts the flip-flop.** longhorn-manager
  sets those four containers' `resources` to exactly what the Setting names and clears the
  rest. `tests/test_longhorn_reconcile.py::test_a_run_reads_and_writes_no_deployment` and
  `tests/test_longhorn_reconcile_render.py::test_the_role_grants_nothing_on_deployments` keep
  this job off them.
- **A reverted H1.6 orphans the CronJob.** `deletionPolicy: Orphan` leaves it running, and it
  writes the same labels the restored `setup/k3s` tasks write. The revert PR names the
  **operator-only** delete.
- **The group names are a contract with the RecurringJobs.** A one-byte change in a label drops
  a volume out of its job with nothing failing. `tests/test_longhorn_reconcile.py` types the
  labels out and holds them equal to `ansible/filter_plugins/longhorn_groups.py`.

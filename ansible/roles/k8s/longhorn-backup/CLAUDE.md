# longhorn-backup — the backup targets, their credentials and the RecurringJobs

The Longhorn backup plane: the credential Secrets `longhorn-b2` and `longhorn-r2`, the
BackupTargets `default` (B2) and `r2`, and the RecurringJobs `daily-backup` and
`weekly-backup-d0` to `weekly-backup-d6`, and the `trim` RecurringJob (H2.2a). It replaced `tasks/longhorn-backup.yml` in
`setup/k3s` (H1.7b). Which target a volume uses is `longhorn-reconcile`'s decision (H1.7a), and
this role only declares the targets.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's defaults, templates, tasks or containers_list entry, or the k3s role's Longhorn tier lists. -->
- **Applied by:** Flux, through `promote` (`owner: flux`); `deploy.sh` refuses
  `--tags=longhorn-backup` with exit 5
- **Route:** none (no `templates/ingressroute.yaml.j2`)
- **Claims:** none (no PVC)
<!-- /generated_from -->

- **Flux applies it, not Ansible** (H1.7b). Its `containers_list` entry carries `owner: flux` and
  `depends_on: [longhorn]`, because `longhorn`'s vendored file defines the BackupTarget and
  RecurringJob CRDs. `tasks/main.yml` holds the include of `k8s/manifests` and no Flux path runs
  it. While `longhorn` is not Ready, this tag applies nothing, so a disarm PR promotes and does
  not reach the cluster. The BackupTarget break-glass below is the lever for that state.
- **The tag is forward-only and never pruned.** `flux_forward_only: true` makes a failed
  revision hold and leave `prod`, because a reset to a revision before a disarm would re-arm B2.
  `flux_prune: false` keeps a render that drops a target or a job from deleting it. Each has a
  `DECIDED:` marker in `defaults/main.yml`. `NEVER_SHED` names the tag, which is untiered.
- **The credentials substitute from `homelab-cluster-vars`.** The two Secret templates are
  `stringData:` with bare `{{ key }}` placeholders, and each BackupTarget URL holds one
  (`${longhorn_b2_bucket}`, `${r2_bucket}`). A rotation reaches the cluster only through
  `cluster_vars.py write` (operator-only). The regions are plain variables, so no filter runs
  over a secret. `scripts/renderer/renderer_lib/secret_checks.py:PLACEHOLDER_KIND_EXEMPT`
  admits the two bucket placeholders in a BackupTarget and nowhere else.
- **A wrong region makes a target unreachable.** Nothing ties `k3s_longhorn_b2_region` to the
  SOPS key `longhorn_b2_endpoint` any more. `probe.py health longhorn-backup` reports a target
  with `status.available` false within one probe run.
- **Five keys are read by no host file.** `k3s_longhorn_backup_armed`, `k3s_longhorn_r2_armed`,
  `k3s_longhorn_backup_cron`, `k3s_longhorn_weekly_backup_minute_hour` and
  `k3s_longhorn_weekly_backup_retain` live here. The backup-health reader derives the arm state
  and the schedules from the live objects, so a flip reaches the cluster through Flux alone.
  ENFORCED:
  `ansible/tests/repo/test_census_rows_roles.py::test_census_row_holds_on_the_tree`, row
  `no-flux-born-key-on-the-host-plane`.
- **A mirrored pair.** `k3s_longhorn_r2_region` here and `k3s_etcd_s3_region` in `setup/k3s`'s
  defaults hold the same value. The off-box etcd snapshot script reads the second.
  ENFORCED:
  `ansible/tests/setup/test_etcd_s3_region_mirrors_the_r2_region.py::test_a_changed_twin_is_flagged`.

## The incident levers (what the arm flags do)

- **Normal path: a disarm PR.** Set `k3s_longhorn_backup_armed: false` (or
  `k3s_longhorn_r2_armed: false`) and land it. The target renders a blank `backupTargetURL`, and
  Flux enforces the blank on every reconcile. Arming unconditionally would resurrect a blanked
  target and restart the B2 transaction-cap spiral, so a blank survives every apply until the
  flag returns to `true`. The flag's comment in `defaults/main.yml` has the spiral's mechanism.
  H1 never sets `flux_suspend` on this tag, and D5.1d refuses a render change to it during a
  suspend.
- **Three cases defeat the normal path.** A disarm cannot promote while `DEPLOY_HOLD` holds
  anything but `none`, a spiral can outrun a merge, and a promoted disarm does not apply while
  `longhorn` is not Ready. `docs/break-glass.md` carries the **operator-only** BackupTarget
  break-glass for all three: annotate both targets with
  `kustomize.toolkit.fluxcd.io/reconcile=disabled`, patch each URL blank, land the disarm PR and
  clear the hold, then wait until `status.lastAppliedOriginRevision` names the disarm commit or a
  descendant before removing the annotation.
- **The monitor stays red while the annotation is in force.** The backup-health reader reports
  an annotated target as a problem and never as a disarm, so a forgotten break-glass cannot sit
  behind a green monitor. `probe.py landing` lists the annotation on both targets.
- **A disarmed target is named, not hidden.** The verdict's `DISARMED:` note carries it.

## Notable

- **`pollInterval` is `0s` on both targets.** A poll costs a metadata GET per volume per retained
  backup, and a failing target retries far faster than its interval
  (`k3s_longhorn_backupstore_poll_interval` and `k3s_longhorn_r2_poll_interval` in
  `defaults/main.yml`).
- **Flux declares three fields of `default`** (`backupTargetURL`, `credentialSecret`,
  `pollInterval`). longhorn-manager creates the object and writes the rest, and the render never
  takes those fields.
- **The flat `weekly-backup` job is gone.** The seven weekday shards replaced it, and the task
  that deleted it left with the file after a read-only `kubectl get recurringjobs` showed no
  such job. Removing a job from this tag leaves it live (`flux_prune: false`), so the PR that
  removes one carries an **operator-only** delete.
- **On a new cluster, bootstrap step 6 applies this tag.** It creates both targets and their
  Secrets, and step 1 confirms that `prod`'s render arms both. Step 7 then forces the
  backupstore sync (`docs/flux-deploy-plane.md`, *Bootstrapping Flux on a rebuilt cluster*).

## What it writes (the autonomous-role contract)

The RecurringJobs are this role's only state-changing actuator. Flux applies them with no human
in the loop, and the contract is written here so a later edit cannot quietly widen it.

- **Scope.** `daily-backup` (group `default`) backs up every volume that has no job of its own,
  so opting OUT is the explicit act: the `no-backup` group, assigned by the `longhorn` role's
  `templates/storageclass-nobackup.yaml.j2`. Each `weekly-backup-d<N>` job backs up the volumes
  `longhorn-reconcile` labels with its shard. The jobs delete old backups through their `retain`
  counts, and only the backups of that job.
- **The `trim` job is no backup tier.** It runs `filesystem-trim` at `k3s_longhorn_trim_cron`
  (06:10 UTC) with `groups: []`, and a volume joins it through the per-job label
  `recurring-job.longhorn.io/trim` that `longhorn-reconcile` sets. A `trim` group would read as
  a backup tier to the restore drill and the reapers, and the `DECIDED:` marker in
  `templates/longhorn-recurringjob.yaml.j2` says why. It deletes no backup.
- **Arming.** `k3s_longhorn_backup_armed` and `k3s_longhorn_r2_armed` blank one target each. The
  retired `k3s_manage_backup_targets` flag existed for a cluster with no real credentials. No
  host set it, and staging, the one such cluster, was retired on 2026-09-29.
- **Abort valve.** The weekly tier is sharded across weekdays, so no day's deletions reach the
  B2 transaction cap (`docs/longhorn-backup-tiering.md`).
- **Next-run review.** `probe.py b2-budget` recomputes the projected spend from the live
  bucket. Run it after adding a volume.

## Editing

- Manifests: `templates/longhorn-b2-secret.yaml.j2`, `templates/longhorn-r2-secret.yaml.j2`,
  `templates/longhorn-backuptarget-default.yaml.j2`, `templates/longhorn-backuptarget-r2.yaml.j2`
  and `templates/longhorn-recurringjob.yaml.j2`.
- Render the tag before you push:
  `uv run python scripts/renderer/homelab_render.py cluster --out <dir> --role longhorn-backup`.

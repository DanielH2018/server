# `setup/k3s` — the cluster's host plane: k3s itself, Longhorn, and the backup crons

The role that turns a host into a k3s node and declares the cluster's own plumbing: the server
install, the agent join, MetalLB, Longhorn with its backup targets, the read-only kubeconfig,
CoreDNS, and the host crons that watch and exercise backups. `daniel-box` is
the server and `daniel-server` an agent (`tasks/agent.yml`).

`ansible/k3s-bringup.yml` applies it, and that is one of the `_BROAD_MANUAL_PREFIXES` playbooks
the GitOps deployer never runs itself, so every change here is a hand apply. The drill's bounds,
the cron-evidence checks, the read-only crons, the control-plane gates and the four measured
incidents are in `docs/k3s-node-plane-crons-and-incidents.md`.

**`--tags k3s` is the MAXIMAL apply, not the one to reach for.** It reapplies every task, and
three gated tasks that touch the control plane become reachable. `docs/k3s-node-plane-crons-and-incidents.md`
names each gate and what trips it. Every task file carries its own tag, so most changes want one
of those, such as `--tags kubeconfig` (`ansible/roles/setup/k3s/tasks/kubeconfig.yml`). The
deployer prints this warning beside the command it suggests, from
`ansible/roles/setup/gitops_deploy/files/deploy_remediation.py:maximal_tag_warning`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`
- **Crons (9):**
  - `Longhorn backup health` — `*/10 * * * *`
  - `Longhorn filesystem trim` — `10 6 * * *`
  - `B2 deletion accounting` — `10 7 * * *`
  - `B2 backup budget listing` — `20 7 * * *`
  - `Longhorn restore drill` — `10 4 * * *`
  - `daniel-box disk health` — `*/10 * * * *`
  - `Release staleness drift check` — `*/30 * * * *`
  - `Off-box etcd snapshot` — `45 2 * * *`
  - `etcd restore drill` — `20 10 * * 1`
- **Timers (2):** `kuma-check-manifest-prune.timer` (`OnCalendar=*-*-* 05:15:00`),
  `kuma-check-live-drift.timer` (`OnCalendar=*-*-* 05:45:00`)
<!-- /generated_from -->

## Layout

`tasks/main.yml` is a list of `import_tasks`, one per topic. Imports are static, so `--tags`
selection sees every task's own tags.
`defaults/main.yml` is long on purpose: each tunable sits under its own explaining paragraph,
some carrying a `DECIDED` marker. `files/` holds the Python the crons run, tested under
`tests/`; `templates/` holds the cron scripts and the manifests this role applies.

Backup tiering, restoring a volume, restoring etcd and the two upgrade runbooks each have a
docs/ page — `docs/longhorn-backup-tiering.md`, `docs/longhorn-disaster-recovery.md`,
`docs/k3s-etcd-restore.md`, `docs/k3s-upgrade.md`, `docs/longhorn-upgrade.md`.

## Autonomous-role contract (the crons that change state with no human in the loop)

`tasks/health-crons.yml` installs a dozen crons; most read the cluster and push a Kuma tile.
Three change state, and so do the Longhorn RecurringJobs `tasks/longhorn.yml` applies. The
authority of those four actuators is written here so a later edit cannot quietly widen it.

- **Scope, per actuator.** The **trim** (`longhorn-trim-volumes.sh`, daily) discards blocks ext4
  already freed and reaches no live data. The **restore drill** (`longhorn-restore-drill.sh`,
  daily) restores ONE volume's newest backup into a NEW volume, proves it in a throwaway pod and
  tears it down, never touching the live volume. The **off-box etcd snapshot**
  (`etcd-snapshot-offbox.sh`, daily) uploads the snapshot and never the cluster token. The
  **RecurringJobs** delete old backups through their `retain` counts, and their `default` group
  covers every volume with no job of its own, so opting OUT is the explicit act — the `no-backup`
  group, assigned by `files/longhorn-storageclass-nobackup.yaml`.
- **The drill's three bounds** — an operator pin, an empty-restore waiver with a size ceiling and
  one retry per failure — are derived on the docs page, each held by a test. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py::test_argv_pin_drills_that_volume_not_the_rotations_pick`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py::test_an_undeclared_volume_still_fails_with_no_files`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_empty_waiver.py::test_a_filled_declared_volume_fails_and_stamps_nothing`,
  `ansible/tests/longhorn/test_longhorn_restore_drill_retry.py::test_a_retry_already_spent_leaves_the_rotation_alone`.
- **Never a cron:** the two reapers delete stranded objects, operator-invoked and dry-run by
  default; scheduling either is out of contract. They live in `scripts/backup/` and run from the
  repo checkout, so this role installs nothing of theirs. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_reap_orphan_never_scheduled.py::test_no_setup_role_schedules_a_reaper`.
- **Mode / arming:** `k3s_manage_health_crons` withholds the whole set,
  `k3s_manage_backup_targets` the R2/B2 targets and the RecurringJobs, and
  `k3s_etcd_restore_drill_armed` the weekly etcd drill, which runs `--list-only` here. Both
  `k3s_manage_*` flags default to `true`, and no host overrides either.
- **Abort valves:** the drill tears down on EVERY exit path, the trim aborts when the node's
  longhorn-manager pod is not ready, and the weekly B2 tier is sharded across weekdays so no
  day's deletions reach the B2 transaction cap.
- **Required evidence:** the trim, the drill and the two B2 accounting crons log every run
  through `logger`, which is evidence only because checks 9 and 10 of `longhorn-backup-health.sh`
  read it, matching each line AS WRITTEN. `docs/k3s-node-plane-crons-and-incidents.md` names the
  regex and the file to edit together. ENFORCED:
  `ansible/roles/setup/k3s/tests/test_longhorn_backup_health_cron_evidence.py::test_cron_evidence_is_flagged_on_a_failed_trim`,
  `ansible/roles/setup/k3s/tests/test_longhorn_backup_health_cron_evidence.py::test_cron_liveness_is_flagged_when_the_cron_entry_is_gone`.
- **Next-run review:** read the B2 ledger and the drill's `/var/log` records before widening
  scope.

## Notable

- **Three traps live here**: the cron PATH omits `/usr/local/bin` where k3s lives, the drift and
  prune checks are staggered (05:15 and 05:45) so two kubectl sweeps never land at once, and
  k3s's own output goes to `/var/log/k3s.log` (not the journal) where the apiserver's OIDC
  authenticator can wedge silently with every probe still green.
  `docs/k3s-node-plane-crons-and-incidents.md` has the detail and the incident evidence for each;
  `ansible/tests/setup/test_k3s_unit_logging.py` holds the log drop-in.

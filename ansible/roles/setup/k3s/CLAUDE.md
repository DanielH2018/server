# `setup/k3s` — the cluster's host plane: k3s itself, Longhorn, and the backup crons

The role that turns a host into a k3s node and keeps the cluster's own plumbing declared:
the server install and its hardening flags, the agent join, MetalLB and the PriorityClasses,
Longhorn plus its recurring-job groups and backup targets, the read-only kubeconfig, the
per-node host config, CoreDNS pointed at Pi-hole — and the host crons that watch and exercise
the backup plane. It is applied by `ansible/k3s-bringup.yml` (`--tags k3s`), one of the
`_BROAD_MANUAL_PREFIXES` playbooks the GitOps deployer never runs itself, so every change
here is a hand apply. `daniel-box` is the server; `daniel-server` joined as an agent on
2026-08-14 (`tasks/agent.yml`); `daniel-stage` is the staging guest on `daniel-server`
(`setup/hypervisor`), whose `host_vars` turn the backup targets and the health crons off (`k3s_manage_backup_targets`,
`k3s_manage_health_crons`), because both would push to prod's Kuma and B2.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates or playbook entry. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`
- **Crons (9):**
  - `Longhorn backup health` — `{{ k3s_longhorn_backup_health_cron_minute }} * * * *`
  - `Longhorn filesystem trim` — `{{ k3s_longhorn_trim_cron_minute }} {{
    k3s_longhorn_trim_cron_hour }} * * *`
  - `B2 deletion accounting` — `{{ k3s_b2_deletion_accounting_cron_minute }} {{
    k3s_b2_deletion_accounting_cron_hour }} * * *`
  - `B2 backup budget listing` — `{{ k3s_b2_budget_cron_minute }} {{ k3s_b2_budget_cron_hour }}
    * * *`
  - `Longhorn restore drill` — `{{ k3s_longhorn_restore_drill_cron.split()[0] }} {{
    k3s_longhorn_restore_drill_cron.split()[1] }} {{ k3s_longhorn_restore_drill_cron.split()[2]
    }} * *`
  - `daniel-box disk health` — `{{ k3s_disk_health_cron_minute }} * * * *`
  - `Release staleness drift check` — `{{ k3s_release_staleness_cron_minute }} * * * *`
  - `Off-box etcd snapshot` — `{{ k3s_etcd_s3_cron_minute }} {{ k3s_etcd_s3_cron_hour }} * * *`
  - `etcd restore drill` — `{{ k3s_etcd_restore_drill_cron.split()[0] }} {{
    k3s_etcd_restore_drill_cron.split()[1] }} * * {{ k3s_etcd_restore_drill_cron.split()[4] }}`
- **Timers (3):** `kuma-check-remember-logs.timer` (`OnCalendar=*-*-* *:{{
  k3s_remember_logs_cron_minute }}:00`), `kuma-check-manifest-prune.timer` (`OnCalendar=*-*-*
  {{ '%02d' | format(k3s_manifest_prune_cron_hour | int) }}:{{ '%02d' |
  format(k3s_manifest_prune_cron_minute | int) }}:00`), `kuma-check-live-drift.timer`
  (`OnCalendar=*-*-* {{ '%02d' | format(k3s_live_drift_cron_hour | int) }}:{{ '%02d' |
  format(k3s_live_drift_cron_minute | int) }}:00`)
<!-- /generated_from -->

## Layout

`tasks/main.yml` is a list of `import_tasks`, one per topic, split on 2026-08-15 when the
single file reached 1058 lines. Imports are static, so `--tags` selection sees every task's own
tags. `defaults/main.yml` is long on purpose: each tunable sits under the paragraph that
explains it, and several carry `DECIDED` markers. `files/` holds the Python the crons run
(`longhorn_backup_health*.py`, `longhorn_reap_*.py`, `live_drift_check.py`,
`manifest_declares.py`) with their tests under `tests/`; `templates/` holds the cron scripts
and the cluster manifests this role applies directly.

Where the long form lives, by topic:

| Topic | Read |
|---|---|
| Backup tiering (R2 daily, B2 weekly), the block-tree cost model, the caps | `docs/longhorn-backup-tiering.md`, ADR-0007, ADR-0008 |
| Restoring a volume, or the whole cluster's storage | `docs/longhorn-disaster-recovery.md` |
| Restoring etcd, and why the cluster token is the one artifact that must survive `daniel-box` | `docs/k3s-etcd-restore.md` |
| Upgrading k3s / Longhorn | `docs/k3s-upgrade.md`, `docs/longhorn-upgrade.md` |
| The deferred-k8s-change gap the release-staleness check closes (issue #947) | `roles/setup/gitops_deploy/CLAUDE.md`, the `#947` bullet under *Safety* |
| Reaping stranded snapshots and backups after a tier move | `roles/k8s/volume-snapshot/CLAUDE.md`, and the docstrings in `files/longhorn_reap_*.py` |

## Autonomous-role contract (the crons that change state with no human in the loop)

`tasks/health-crons.yml` installs a dozen crons, and most of them read the cluster and push
a Kuma tile. Three of those crons change state, and so do the Longhorn RecurringJobs
`tasks/longhorn.yml` applies as a manifest. The authority of those four actuators is written
here so a later edit cannot quietly widen it — the same reason `k8s/autofix-bridge` and
`initial_setup` carry this section.

- **Scope, per actuator:**
  - **Longhorn filesystem trim** (`longhorn-trim-volumes.sh`, daily
    `k3s_longhorn_trim_cron_hour`, as `sys_user`): issues a discard on every Longhorn volume
    so blocks ext4 already freed stop being backed up. It cannot reach live data, and it
    costs no B2 transactions — the work is entirely local.
  - **Longhorn restore drill** (`longhorn-restore-drill.sh`, daily
    `k3s_longhorn_restore_drill_cron`, as root): restores the newest backup of ONE eligible
    volume into a NEW volume, mounts it in a throwaway pod, checks the data is real, and tears
    everything down. The live volume is never touched. Eligibility is the
    `recurring-job-group.longhorn.io/*` label, with the `no-backup` group excluded by name;
    the volume is chosen least-recently-attempted, not least-recently-succeeded, so one
    permanently broken volume cannot starve the rest. To drill one volume on demand, pass its
    PVC name: `sudo /usr/local/bin/longhorn-restore-drill.sh <pvc>` (or `RESTORE_DRILL_PIN=<pvc>`
    for a caller that cannot pass an argument). A hand run stamps `success/<pvc>` but not
    `last-success`, so it proves the volume without hiding a dead nightly cron from check 7,
    and it leaves the published candidate list whole so check 8's coverage window keeps its
    size. Backdating a stamp under `attempts/` to steer the rotation is no longer the way in
    (#2183). ENFORCED:
    `ansible/tests/longhorn/test_longhorn_restore_drill_operator_pin.py::test_argv_pin_drills_that_volume_not_the_rotations_pick`.
    A volume whose content is legitimately EMPTY is declared in
    `ansible/roles/setup/k3s/defaults/main.yml:k3s_longhorn_restore_drill_empty_ok_pvcs`, and
    the drill waives its two content assertions for that name alone — everything else it
    proves for that volume it still proves. n8n-files restored to `files=0` on 2026-08-30,
    could not be re-drilled for another rotation, and check 8 paged `not restore-proven in
    31d`. ENFORCED:
    `ansible/tests/longhorn/test_longhorn_restore_drill_byte_floor.py::test_an_undeclared_volume_still_fails_with_no_files`.
    A FAILED attempt is re-drilled the next night, ahead of the rotation, once per failure
    (#2270). Selection is least-recently-attempted and the attempt stamp is refreshed whatever
    the outcome, so before this a failed volume waited a full 26-night cycle against check 8's
    31-day window and paged first. The bound is a marker under `retries/`, stamped with the
    attempt: a volume that fails its retry waits for its own rotation slot, so a permanently
    broken volume costs two nights a cycle rather than every night. ENFORCED:
    `ansible/tests/longhorn/test_longhorn_restore_drill_retry.py::test_a_retry_already_spent_leaves_the_rotation_alone`.
  - **Off-box etcd snapshot** (`etcd-snapshot-offbox.sh`, daily `k3s_etcd_s3_cron_hour`, as
    root): takes a k3s etcd snapshot and uploads it to R2. It uploads the snapshot only —
    never the cluster token, because the token beside the snapshot would undo
    `--secrets-encryption` for anyone holding the bucket.
  - **Longhorn RecurringJobs** (`templates/longhorn-recurringjob.yaml.j2`, applied by
    `tasks/longhorn.yml`): the cluster-side snapshot and backup jobs, whose `retain` counts
    are what delete old backups. A `default`-group job covers every volume that has no job of
    its own; opting a volume OUT is the explicit act — the `no-backup` group,
    which `files/longhorn-storageclass-nobackup.yaml` assigns.
- **Never a cron:** `longhorn-reap-orphan-backups.sh` and `longhorn-reap-orphan-snapshots.sh`
  delete stranded objects and are operator-invoked only, dry-run by default, `--apply` to
  delete, with a per-run deletion cap. Scheduling either is out of contract. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_reap_orphan_never_scheduled.py::test_no_setup_role_schedules_a_reaper`
  reads every `cron:` task, `kuma_check_timer.yml` import and systemd unit template under
  `ansible/roles/setup/` and fails on either name.
- **Mode / arming:** `k3s_manage_health_crons` (staging: `false`) installs or withholds the
  whole set; `k3s_manage_backup_targets` does the same for the R2/B2 targets and the
  RecurringJobs; `k3s_etcd_restore_drill_armed` gates the weekly `etcd restore drill` cron,
  which on this host runs `--list-only` — the FULL restore drill runs in a throwaway guest on
  `daniel-server` (`setup/hypervisor`), because it cannot pass beside a live k3s.
- **Authoritative sources:** Longhorn's own Volume, Backup and Snapshot CRs read through
  the cluster; B2/R2 listings read from the buckets (`probe.py b2-deletions`, the budget
  listing). Never a cached count.
- **Abort valves:** the restore drill tears down on EVERY exit path, so a failed run leaves no
  attached volume or restored bytes behind; the trim aborts when the node's longhorn-manager
  pod is not ready and touches nothing else; the weekly B2 tier is sharded across weekdays so
  no single day's backup deletions reach the B2 transaction cap
  (`docs/longhorn-backup-tiering.md`).
- **Required evidence:** the trim and the drill log every run through `logger` (tags
  `longhorn-trim`, `longhorn-restore-drill`, readable in Loki). The drill also stamps
  `ATTEMPT_DIR` on every run and `SUCCESS_DIR` only after the assertions pass; check 7 of
  `longhorn-backup-health.sh` reads those and pushes DOWN when the drill stops running — a
  drill that silently stops looks identical to one never scheduled, which is how the
  hand-run drill of 2026-08-16 went unrepeated until the cron existed. The etcd snapshot
  pushes its own Kuma tile through `kuma-push-lib.sh`.
- **Next-run review:** before widening scope (a second drilled volume per day, a shorter
  RecurringJob retention, scheduling a reap), read the B2 ledger and the drill's
  `/var/log` records for what the last week actually cost and what actually failed.

The read-only crons in the same file — `Longhorn backup health`, `daniel-box disk health`,
`remember log rotation health`, `Manifest prune drift check`, `Release staleness drift
check`, `Live object drift check`, `B2 deletion accounting`, `B2 backup budget listing`, and
the `--list-only` etcd drill — read the cluster or the bucket and write nothing to either.
The heartbeats push a Kuma tile through `kuma-push-lib.sh`; the B2 accounting pair appends to
the local ledger, which is bookkeeping, not state.

Three of those heartbeats are kuma-check timers rather than crons since 2026-09-19:
`remember log rotation health`, `Manifest prune drift check` and `Live object drift check`
import [[common]]'s `kuma_check_timer.yml`. Each script exits 1 after it pushes `down`, and
the service's `Restart=on-failure` reruns it (15 min for the hourly check, 30 min for the
daily ones) until it exits 0, so a red tile clears when the fault does rather than at the
next slot. The timers are `Persistent=true`; the two daily checks carry the boot grace so a
catch-up run at boot exits 1 without a verdict and the restart carries the real one.
`systemctl status kuma-check-<name>` shows `auto-restart` while red. Manifest prune's
healthchecks.io `/fail` ping repeats on every rerun; healthchecks notifies on a status change,
so a check already down is not paged again.

## Notable

- **k3s's own output is in `/var/log/k3s.log`, not the journal.** journald stores nothing
  below notice on these hosts and every line k3s writes is priority info — logrus and klog
  emit plain text to stderr with no `<N>` prefix, so INFO, WARN and FATA all take the unit's
  `SyslogLevel=info` default. On 2026-09-09 k3s crash-looped ~3000 times over 5h08m and the
  journal kept only systemd's `status=1/FAILURE` lines (#1918). `tasks/unit-logging.yml`
  writes a `StandardOutput=append:` drop-in for `k3s.service` and `k3s-agent.service`, plus
  a `copytruncate` logrotate stanza — truncate, because systemd holds the fd and a rename
  would leave the unit writing to the rotated inode. A drop-in rather than the unit because
  `k3s-install.sh` rewrites the unit file wholesale. Raising the unit's `SyslogLevel` to
  notice was the alternative and was rejected: notice passes the rsyslog info filter, so
  every k3s line would land in `/var/log/syslog` and ship to Loki.
  `ansible/tests/setup/test_k3s_unit_logging.py` holds both node types to it.
- **The release-staleness check is the durable half of a one-shot Discord page.** When the
  deployer defers a k8s change it cannot auto-apply (`deploy_alerts.alert_deferred`'s
  `cs.k8s` branch), it fast-forwards the tree and pages once per SHA; the ff-merge clears the
  deployer's own `behind_since`, so nothing else says the cluster is still running old
  manifests. `release-staleness-check.sh` runs `probe.py releases --stale-only` every
  `k3s_release_staleness_cron_minute` and pushes the tile down while any service's applied
  commit sits behind `origin/master` under its role paths, or under an inventory key or
  shared macro its render reads (#1993). A merge younger than
  `k3s_release_staleness_grace_minutes` is named in the `up` message and not counted, so a
  landing still deploying its own merge does not page. The full account, including why it runs as
  `sys_user` and not root, is the `#947` bullet in `roles/setup/gitops_deploy/CLAUDE.md`.
  **The DOWN msg carries the stale services' names and a count, not their reasons** (#2013).
  A refused narrowing marks the whole fleet stale, and the per-service reasons then ran to
  ~7,500 chars; Kuma puts the msg into a Discord embed field capped at 1024 chars and never
  truncates, so Discord rejected the DOWN with HTTP 400 on 2026-09-17 and 2026-09-18 and the
  page reached nobody. `kuma_push` (kuma-push-lib.sh) and the bridge's `net.push` both cap
  the msg at 900 chars as the class fix; the reasons are `probe.py releases --stale-only`.
- **Cron's PATH omits `/usr/local/bin`, where k3s lives.** Every script here sets its own
  PATH; a new one that does not dies on `command -v k3s` and, if it pushes its heartbeat
  before the check, reads permanently green.
- **The drift checks and the prune check are staggered on purpose** (05:15 and 05:45) so two
  full kubectl sweeps do not land on the API server at once. Keep a new sweep off those
  minutes.

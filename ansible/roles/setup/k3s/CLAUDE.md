# `setup/k3s` — the cluster's host plane: k3s itself, Longhorn, and the backup crons

The role that turns a host into a k3s node and declares the cluster's own plumbing: the server
install, the packages Longhorn needs on the node, the read-only kubeconfig, the
marker that releases k3s's CoreDNS addon, and the host crons that watch and exercise backups.
`daniel-box` is the server, and `daniel-server` an agent whose half is its host package's. The CoreDNS objects
belong to the k8s `coredns` role and the Longhorn install to the k8s `longhorn` role, and Flux
applies both. The Longhorn backup targets, their credentials and the RecurringJobs are the
`longhorn-backup` role's (H1.7b).

`ansible/k3s-bringup.yml` applies it, and that is one of the `_BROAD_MANUAL_PREFIXES` playbooks
the GitOps deployer never runs itself, so every change here is a hand apply. The cron-evidence checks, the read-only crons, the control-plane gates and the four measured
incidents are in `docs/k3s-node-plane-crons-and-incidents.md`.

**`--tags k3s` is the MAXIMAL apply, not the one to reach for.** It reapplies every task, and
two gated tasks that touch the control plane become reachable. `docs/k3s-node-plane-crons-and-incidents.md`
names each gate and what trips it. Every task file carries its own tag, so most changes want one
of those, such as `--tags kubeconfig` (`ansible/roles/setup/k3s/tasks/kubeconfig.yml`). The
deployer prints this warning beside the command it suggests, from
`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py:maximal_tag_warning`.

**`--tags k3s` never reaches an agent node, and nothing here needs to.** daniel-server's agent
half is its package's since H7.34: `hosts/components/k3s-agent/` ships the Longhorn packages, the
multipath drop-in, the inotify key, the join rules and the address pin, and
`hosts/bootstrap-host.sh` installs and joins k3s. `tasks/agent.yml` and `tasks/agent_verify.yml` are each one
task that refuses, because `k3s-bringup.yml`'s join plays name them until H7.20a. `tasks/static-address.yml` is
daniel-box's pin, and each of its tasks skips `k3s_packaged_hosts`'s hosts. The deployer's join
and pin printers (`ansible/roles/setup/gitops_deploy/files/gitops_markers.py:agent_join_cmds`) therefore name no
agent command for this role.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`;
  `k3s-bringup.yml --tags "node-address"`
- **Crons (6):**
  - `Longhorn backup health` — `*/10 * * * *`
  - `B2 deletion accounting` — `10 7 * * *`
  - `B2 backup budget listing` — `20 7 * * *`
  - `daniel-box disk health` — `*/10 * * * *`
  - `etcd token drift` — `45 2 * * *`
  - `etcd restore drill` — `20 10 * * 1`
<!-- /generated_from -->

## Layout

`tasks/main.yml` is a list of `import_tasks`, one per topic. Imports are static, so `--tags`
selection sees every task's own tags.
`defaults/main.yml` is long on purpose: each tunable sits under its own explaining paragraph,
some carrying a `DECIDED` marker. `files/` holds the Python the crons run, tested under
`tests/`; `templates/` holds the cron scripts and the manifests this role applies.

**The k3s pin is not in this role's defaults.** `k3s_version`, `k3s_install_script_url` and
`k3s_install_script_sha256` live in `ansible/roles/k8s/system-upgrade/defaults/main.yml`
since H6.1b, so a pin change routes to the Flux-owned `system-upgrade` tag and writes no
`manual_plane` line for this role. `tasks/server.yml` reads the file with
`include_vars`. `SYSTEM_UPGRADE_DEFAULTS` in `scripts/lib/repo_paths.py` is the Python readers' path.
The server's install task runs only for a missing binary (since H6.4b). SUC's `k3s-server` and `k3s-agent` Plans
are the only writers of each node's version.

Backup tiering, restoring a volume, restoring etcd and the two upgrade runbooks each have a
docs/ page — `docs/longhorn-backup-tiering.md`, `docs/longhorn-disaster-recovery.md`,
`docs/k3s-etcd-restore.md`, `docs/k3s-upgrade.md`, `docs/longhorn-upgrade.md`.

## Autonomous-role contract (the crons that change state with no human in the loop)

`tasks/health-crons.yml` installs a dozen crons; most read the cluster and write a check
textfile that a Prometheus rule reads.
None changes the cluster or a backup store. Their limits are written here so a later edit cannot
quietly widen them. Five state-changing actuators left this role. The volume-label reconcile left
at H1.6, and its contract is the `longhorn-reconcile` role's `CLAUDE.md`. The Longhorn RecurringJobs
left at H1.7b, and the filesystem trim became one of them at H2.2c; their contract is the
`longhorn-backup` role's `CLAUDE.md`. The restore drill left at H2.3d, and its contract is the
`longhorn-restore-drill` role's `CLAUDE.md`. The off-box etcd upload left at H2.8b, when k3s's
native schedule replaced it; `k3s_server_config` in `hosts/components/k3s-server/component.yml`
holds that schedule.

- **Scope.** The crons read and report. `etcd-token-drift.sh` compares the cluster token with the
  operator's stamp and writes neither.
- **Never a cron:** the two reapers delete stranded objects, operator-invoked and dry-run by
  default; scheduling either is out of contract. They are the `backups` and `snapshots`
  subcommands of `scripts/backup/longhorn_reap.py` and run from the repo checkout, so this role
  installs nothing of theirs. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py::test_no_setup_role_schedules_a_reaper`.
- **Mode / arming:** `k3s_manage_health_crons` withholds the whole set, and
  `k3s_etcd_restore_drill_armed` the weekly etcd drill, which runs `--list-only` here. The
  `k3s_manage_health_crons` flag defaults to `true`, and no host overrides it.
- **Required evidence:** checks 9 and 10 of `longhorn-backup-health.sh` read the trim's pod
  log for a per-volume failure and its CronJob for a stopped schedule. They read the B2 deletion
  accounting cron's `logger` lines the same way, matching each line AS WRITTEN. `docs/k3s-node-plane-crons-and-incidents.md`
  names the regex and the file to edit together. ENFORCED:
  `scripts/lib/tests/test_longhorn_backup_health_cron_evidence.py::test_cron_evidence_is_flagged_on_a_failed_trim`,
  `scripts/lib/tests/test_longhorn_backup_health_cron_evidence.py::test_trim_is_flagged_when_its_cronjob_is_gone`.
  Check 12 reads k3s's newest S3 snapshot, against a bound from the installed drop-in's schedule.
  ENFORCED: `scripts/lib/tests/test_longhorn_etcd_s3.py::test_a_snapshot_past_the_bound_is_flagged`.
- **Next-run review:** read the B2 ledger before widening scope.

## Notable

- **Two traps live here**: the cron PATH omits `/usr/local/bin` where k3s lives, and
  k3s's own output goes to `/var/log/k3s.log` (not the journal) where the apiserver's OIDC
  authenticator can wedge silently with every probe still green.
  `docs/k3s-node-plane-crons-and-incidents.md` has the detail and the incident evidence for each;
  `ansible/tests/setup/test_k3s_server_component.py` and
  `ansible/tests/setup/test_k3s_agent_component.py` hold the two nodes' log drop-ins.
- **The server's same-path files are the `homelab-daniel-box` package's since H6.4a**, so no
  task here writes them: the audit policy, the authentication config, `registries.yaml`, the
  logging drop-in with its logrotate file, and the CoreDNS `.skip` marker. They live in
  `hosts/components/k3s-server/`, and `homelab-k3s-restart` restarts k3s once per changed hash
  of the ones k3s reads at startup. `ansible/tests/setup/test_k3s_server_component.py` holds
  the component. The package is forward-only: a revert deletes files the flags name.
- **The agent's same-path files are the `homelab-daniel-server` package's since H6.5a**:
  `registries.yaml`, the logging drop-in, its logrotate file and the log file's `tmpfiles.d`
  entry, in `hosts/components/k3s-agent/`. daniel-server's package lists no `k3s-restart`, so a
  change to them goes live at the agent's next start.
  `ansible/tests/setup/test_k3s_agent_component.py` holds the component.
- **The server's flags are the same package's since H6.4b.** `k3s_server_config` in
  `hosts/components/k3s-server/component.yml` renders into
  `/etc/rancher/k3s/config.yaml.d/50-homelab.yaml`, and the unit drop-in runs a bare `k3s
  server`. `tasks/server.yml` therefore passes the installer only `server`, on a host with no
  binary, after it asserts the package installed the flags.
  `ansible/tests/setup/test_k3s_server_flags_migration.py` held the move to the old flags.
- **The agent's flags are daniel-server's package's since H6.5b.** `k3s_agent_config` in
  `hosts/components/k3s-agent/component.yml` renders into the same config.yaml.d path on
  daniel-server, and the agent's unit drop-in runs a bare `k3s agent`. `hosts/bootstrap-host.sh`
  passes the installer only `agent`. A flag change goes live at the agent's
  next start, because daniel-server's package lists no `k3s-restart`.
  `ansible/tests/setup/test_k3s_agent_flags_migration.py` holds the move to the live flags.
- **No task here writes an object Flux owns, and a new cluster writer is refused.** The
  `k3s-role-cluster-writes-are-counted` census row fails on a task that writes the cluster from
  any file outside `ansible/tests/repo/_k3s_write_verbs_row.py:K3S_WRITE_ALLOW`, which names
  `storage_smoke.yml` only. Hand the objects to a Flux tag instead.
  The stale-checkout guard that H1 ran beside it was retired in #5957 once H1.7b had removed its
  last row. A checkout older than H1.7b still carries its own guard, so this tree's empty copy
  protected nothing. To revive it, take it from #5316 in git history.
- **The two heartbeat checks write a textfile verdict and push nothing (M6 batch B1).** Longhorn
  backup health and the cluster-token drift check each write the three homelab_check_* series to
  node_exporter's textfile directory. Rules in observability's host-checks.yaml page the files through
  Alertmanager. No check reads a push credential or pushes: the tasks that rendered the two
  credential files now remove them, and a skipped boot-grace run writes nothing. The deadline of a
  */N cron derives from its minute variable, and the daily check reads
  k3s_daily_check_deadline_s. A change to a schedule moves its deadline, and the rule's literal
  follows it (the pin is ansible/tests/services/test_b1_rule_literals.py). The no-push guard is
  ansible/tests/setup/test_k3s_cron_textfiles.py.

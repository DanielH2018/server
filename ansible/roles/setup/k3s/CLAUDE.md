# `setup/k3s` — the cluster's host plane: k3s itself, Longhorn, and the backup crons

The role that turns a host into a k3s node and declares the cluster's own plumbing: the server
install, the agent join, the packages Longhorn needs on the node, the read-only kubeconfig, the
marker that releases k3s's CoreDNS addon, and the host crons that watch and exercise backups.
`daniel-box` is the server and `daniel-server` an agent (`tasks/agent.yml`). The CoreDNS objects
belong to the k8s `coredns` role and the Longhorn install to the k8s `longhorn` role, and Flux
applies both. The Longhorn backup targets, their credentials and the RecurringJobs are the
`longhorn-backup` role's (H1.7b).

`ansible/k3s-bringup.yml` applies it, and that is one of the `_BROAD_MANUAL_PREFIXES` playbooks
the GitOps deployer never runs itself, so every change here is a hand apply. The cron-evidence checks, the read-only crons, the control-plane gates and the four measured
incidents are in `docs/k3s-node-plane-crons-and-incidents.md`.

**`--tags k3s` is the MAXIMAL apply, not the one to reach for.** It reapplies every task, and
three gated tasks that touch the control plane become reachable. `docs/k3s-node-plane-crons-and-incidents.md`
names each gate and what trips it. Every task file carries its own tag, so most changes want one
of those, such as `--tags kubeconfig` (`ansible/roles/setup/k3s/tasks/kubeconfig.yml`). The
deployer prints this warning beside the command it suggests, from
`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py:maximal_tag_warning`.

**`--tags k3s` never reaches an agent node.** `tasks/agent.yml` runs only from the opt-in join
plays, so a change it reads, such as `templates/registries.yaml.j2`, also needs
`uv run ansible-playbook ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent`
before the role's `manual_plane` line is cleared. The deployer prints that command for each
host in `k3s_agent_hosts` (`ansible/inventory/group_vars/all.yml`), from
`ansible/roles/setup/gitops_deploy/files/gitops_markers.py:agent_join_cmds`. It prints it beside
the whole-role tag, and for any narrowed row holding `k3s_agent`. `narrow_setup.py` reads the
join play's `include_role`, so `tasks/agent.yml` alone narrows to `k3s_agent` and prints the join
with no server command. `templates/registries.yaml.j2` narrows to `k3s_server` plus the join.
The deployer records the hosts on the line as `agent_hosts`, so the SessionStart banner,
`probe.py gitops` and monitor-bridge print the same commands from the row.

**`--tags node-address` pins daniel-box alone.** `tasks/static-address.yml` also runs from the
opt-in pin play, which matches no host without `-e pin_agent_address=<host>` and runs ON the
agent. So a change reaching that file, and the whole-role tag, also print
`uv run ansible-playbook ansible/k3s-bringup.yml -e pin_agent_address=daniel-server --tags node-address`
for each agent host, with the host to run it on. The server command keeps `node-address`,
because `tasks/main.yml` imports the same file. The data is
`ansible/roles/setup/gitops_deploy/files/gitops_markers.py:AGENT_PIN_TAGS`, and the commands
come from `ansible/roles/setup/gitops_deploy/files/gitops_markers.py:agent_pin_cmds`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`;
  `k3s-bringup.yml --tags "node-address"`
- **Crons (8):**
  - `Longhorn backup health` — `*/10 * * * *`
  - `Longhorn filesystem trim` — `10 6 * * *`
  - `B2 deletion accounting` — `10 7 * * *`
  - `B2 backup budget listing` — `20 7 * * *`
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

**The k3s pin is not in this role's defaults.** `k3s_version`, `k3s_install_script_url` and
`k3s_install_script_sha256` live in `ansible/roles/k8s/system-upgrade/defaults/main.yml`
since H6.1b, so a pin change routes to the Flux-owned `system-upgrade` tag and writes no
`manual_plane` line for this role. `tasks/server.yml` and `tasks/agent.yml` read the file with
`include_vars`. `SYSTEM_UPGRADE_DEFAULTS` in `scripts/lib/repo_paths.py` is the Python readers' path.

Backup tiering, restoring a volume, restoring etcd and the two upgrade runbooks each have a
docs/ page — `docs/longhorn-backup-tiering.md`, `docs/longhorn-disaster-recovery.md`,
`docs/k3s-etcd-restore.md`, `docs/k3s-upgrade.md`, `docs/longhorn-upgrade.md`.

## Autonomous-role contract (the crons that change state with no human in the loop)

`tasks/health-crons.yml` installs a dozen crons; most read the cluster and push a Kuma tile.
Two change state. The authority of those two actuators is written here so a later edit
cannot quietly widen it. Three state-changing actuators left this role. The volume-label
reconcile left at H1.6, and its contract is the `longhorn-reconcile` role's `CLAUDE.md`. The
Longhorn RecurringJobs left at H1.7b, and their contract is the `longhorn-backup` role's
`CLAUDE.md`. The restore drill left at H2.3d, and its contract is the `longhorn-restore-drill`
role's `CLAUDE.md`.

- **Scope, per actuator.** The **trim** (`longhorn-trim-volumes.sh`, daily) discards blocks ext4
  already freed and reaches no live data. The **off-box etcd snapshot**
  (`etcd-snapshot-offbox.sh`, daily) uploads the snapshot and never the cluster token.
- **Never a cron:** the two reapers delete stranded objects, operator-invoked and dry-run by
  default; scheduling either is out of contract. They are the `backups` and `snapshots`
  subcommands of `scripts/backup/longhorn_reap.py` and run from the repo checkout, so this role
  installs nothing of theirs. ENFORCED:
  `ansible/tests/longhorn/test_longhorn_reap_never_scheduled.py::test_no_setup_role_schedules_a_reaper`.
- **Mode / arming:** `k3s_manage_health_crons` withholds the whole set, and
  `k3s_etcd_restore_drill_armed` the weekly etcd drill, which runs `--list-only` here. The
  `k3s_manage_health_crons` flag defaults to `true`, and no host overrides it.
- **Abort valves:** the trim aborts when the node's
  longhorn-manager pod is not ready or the volume list fails, and the weekly B2 tier is sharded across weekdays so no
  day's deletions reach the B2 transaction cap.
- **Required evidence:** the trim and the two B2 accounting crons log every run
  through `logger`, which is evidence only because checks 9 and 10 of `longhorn-backup-health.sh`
  read it, matching each line AS WRITTEN. `docs/k3s-node-plane-crons-and-incidents.md` names the
  regex and the file to edit together. ENFORCED:
  `scripts/lib/tests/test_longhorn_backup_health_cron_evidence.py::test_cron_evidence_is_flagged_on_a_failed_trim`,
  `scripts/lib/tests/test_longhorn_backup_health_cron_evidence.py::test_cron_liveness_is_flagged_when_the_cron_entry_is_gone`.
- **Next-run review:** read the B2 ledger before widening scope.

## Notable

- **Three traps live here**: the cron PATH omits `/usr/local/bin` where k3s lives, the drift and
  prune checks are staggered (05:15 and 05:45) so two kubectl sweeps never land at once, and
  k3s's own output goes to `/var/log/k3s.log` (not the journal) where the apiserver's OIDC
  authenticator can wedge silently with every probe still green.
  `docs/k3s-node-plane-crons-and-incidents.md` has the detail and the incident evidence for each;
  `ansible/tests/setup/test_k3s_unit_logging.py` holds the log drop-in.
- **Both drift checks know an object Flux applies, and neither reads its label alone.** The
  prune check skips an object labelled `kustomize.toolkit.fluxcd.io/name=<n>` only while a
  Kustomization `<n>` exists in `flux-system`. It reads that list once per run, and a failed
  read skips nothing. The live drift check takes a `kustomize-controller` `Apply` entry as
  server-side only on an object without `last-applied-configuration`. Flux strips that
  annotation on every apply, so its presence means Ansible applied the object after a
  handback. A handed-back object keeps both the label and Flux's `managedFields` entry, which
  is why each check also reads live state. ENFORCED:
  `ansible/roles/setup/k3s/tests/test_manifest_prune_check_flux_skip.py::test_a_labelled_object_whose_kustomization_is_gone_is_flagged`,
  `ansible/roles/setup/k3s/tests/test_live_drift_check.py::test_a_handed_back_flux_object_is_compared`.
- **No task here writes an object Flux owns, and a new cluster writer is refused.** The
  `k3s-role-cluster-writes-are-counted` census row fails on a task that writes the cluster from
  any file outside `ansible/tests/repo/_k3s_write_verbs_row.py:K3S_WRITE_ALLOW`, which names
  `storage_smoke.yml` and `agent_verify.yml` only. Hand the objects to a Flux tag instead.
  The stale-checkout guard that H1 ran beside it was retired in #5957 once H1.7b had removed its
  last row. A checkout older than H1.7b still carries its own guard, so this tree's empty copy
  protected nothing. To revive it, take it from #5316 in git history.
- **The five heartbeat checks also write a textfile verdict (M6 batch B1).** Longhorn backup
  health, the off-box etcd snapshot, the manifest-prune and release-staleness checks, and the
  live-drift check each write the three homelab_check_* series to node_exporter's textfile
  directory before their Kuma push. Shadow rules in observability's host-checks.yaml read the
  files until B1's cutover. The deadline of a */N cron derives from its minute variable, and the
  three daily checks share k3s_daily_check_deadline_s. A change to a schedule moves its deadline,
  and the rule's literal follows it (the pin is ansible/tests/services/test_b1_rule_literals.py).

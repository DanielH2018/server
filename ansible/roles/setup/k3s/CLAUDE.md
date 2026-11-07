# `setup/k3s` — the cluster's host plane: k3s itself, Longhorn, and the backup crons

The role that installs the k3s server on `daniel-box` and joins `daniel-server` as an agent
(`tasks/agent.yml`), and writes the marker that releases k3s's CoreDNS addon. Everything else the
server node needs is in `homelab-daniel-box` since H7.35: Longhorn's packages, the read-only
kubeconfig, the join rules and the address pin in `hosts/components/k3s-server/`, the resolver and
the host forwarder in `hosts/components/k3s-host-dns/`, and the backup and disk checks in
`hosts/components/k3s-host-checks/`. The CoreDNS objects belong to the k8s `coredns` role and the
Longhorn install to the k8s `longhorn` role, and Flux applies both. The Longhorn backup targets,
their credentials and the RecurringJobs are the `longhorn-backup` role's (H1.7b).

`ansible/k3s-bringup.yml` applies it, and that is one of the `_BROAD_MANUAL_PREFIXES` playbooks
the GitOps deployer never runs itself, so every change here is a hand apply. The control-plane
gates and the four measured incidents are in `docs/k3s-node-plane-crons-and-incidents.md`.

**`--tags k3s` is the MAXIMAL apply, not the one to reach for.** It reapplies every task, and
two gated tasks that touch the control plane become reachable. `docs/k3s-node-plane-crons-and-incidents.md`
names each gate and what trips it. Every task file carries its own tag, so most changes want one
of those, such as `--tags coredns` (`ansible/roles/setup/k3s/tasks/coredns.yml`). The
deployer prints this warning beside the command it suggests, from
`ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_remediation.py:maximal_tag_warning`.

**`--tags k3s` never reaches an agent node.** `tasks/agent.yml` runs only from the opt-in join
plays, so a change to it also needs
`uv run ansible-playbook ansible/k3s-bringup.yml -e join_agent=daniel-server --tags k3s_agent`
before the role's `manual_plane` line is cleared. The deployer prints that command for each
host in `k3s_agent_hosts` (`ansible/inventory/group_vars/all.yml`), from
`ansible/roles/setup/gitops_deploy/files/gitops_markers.py:agent_join_cmds`. It prints it beside
the whole-role tag, and for any narrowed row holding `k3s_agent`. `narrow_setup.py` reads the
join play's `include_role`, so `tasks/agent.yml` alone narrows to `k3s_agent` and prints the join
with no server command. The deployer records the hosts on the line as `agent_hosts`, so the
SessionStart banner, `probe.py gitops` and monitor-bridge print the same commands from the row.

**`--tags node-address` writes nothing on daniel-box.** daniel-box's pin is its package's since
H7.35, and `k3s_packaged_hosts` in `defaults/main.yml` skips each task of
`tasks/static-address.yml` there. The file runs from the opt-in pin play, which matches no host
without `-e pin_agent_address=<host>` and runs ON the agent. So a change reaching that file, and
the whole-role tag, also print
`uv run ansible-playbook ansible/k3s-bringup.yml -e pin_agent_address=daniel-server --tags node-address`
for each agent host, with the host to run it on. The data is
`ansible/roles/setup/gitops_deploy/files/gitops_markers.py:AGENT_PIN_TAGS`, and the commands
come from `ansible/roles/setup/gitops_deploy/files/gitops_markers.py:agent_pin_cmds`.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `k3s-bringup.yml --tags "k3s"`; `k3s-bringup.yml --tags "k3s_agent"`;
  `k3s-bringup.yml --tags "node-address"`
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Layout

`tasks/main.yml` is a list of `import_tasks`, one per topic. Imports are static, so `--tags`
selection sees every task's own tags.
`defaults/main.yml` is long on purpose: each tunable sits under its own explaining paragraph,
some carrying a `DECIDED` marker. `templates/` holds the address pin's two netplan files, which
daniel-box's package renders in place through `role: k3s` while daniel-server's pin play applies
them.

**The k3s pin is not in this role's defaults.** `k3s_version`, `k3s_install_script_url` and
`k3s_install_script_sha256` live in `ansible/roles/k8s/system-upgrade/defaults/main.yml`
since H6.1b, so a pin change routes to the Flux-owned `system-upgrade` tag and writes no
`manual_plane` line for this role. `tasks/server.yml` and `tasks/agent.yml` read the file with
`include_vars`. `SYSTEM_UPGRADE_DEFAULTS` in `scripts/lib/repo_paths.py` is the Python readers' path.
The server's install task runs only for a missing binary (since H6.4b), and so does the
agent's (since H6.5b). SUC's `k3s-server` and `k3s-agent` Plans
are the only writers of each node's version.

Backup tiering, restoring a volume, restoring etcd and the two upgrade runbooks each have a
docs/ page — `docs/longhorn-backup-tiering.md`, `docs/longhorn-disaster-recovery.md`,
`docs/k3s-etcd-restore.md`, `docs/k3s-upgrade.md`, `docs/longhorn-upgrade.md`.

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
  change to them goes live at the agent's next start. `tasks/agent.yml` keeps the join, the
  firewall, the Longhorn prerequisites and the installer.
  `ansible/tests/setup/test_k3s_agent_component.py` holds the component.
- **The server's flags are the same package's since H6.4b.** `k3s_server_config` in
  `hosts/components/k3s-server/component.yml` renders into
  `/etc/rancher/k3s/config.yaml.d/50-homelab.yaml`, and the unit drop-in runs a bare `k3s
  server`. `tasks/server.yml` therefore passes the installer only `server`, on a host with no
  binary, after it asserts the package installed the flags.
  `ansible/tests/setup/test_k3s_server_flags_migration.py` held the move to the old flags.
- **The agent's flags are daniel-server's package's since H6.5b.** `k3s_agent_config` in
  `hosts/components/k3s-agent/component.yml` renders into the same config.yaml.d path on
  daniel-server, and the agent's unit drop-in runs a bare `k3s agent`. `tasks/agent.yml` passes
  the installer only `agent`, on a host with no binary. A flag change goes live at the agent's
  next start, because daniel-server's package lists no `k3s-restart`.
  `ansible/tests/setup/test_k3s_agent_flags_migration.py` holds the move to the live flags.
- **No task here writes an object Flux owns, and a new cluster writer is refused.** The
  `k3s-role-cluster-writes-are-counted` census row fails on a task that writes the cluster from
  any file outside `ansible/tests/repo/_k3s_write_verbs_row.py:K3S_WRITE_ALLOW`, which names
  `storage_smoke.yml` and `agent_verify.yml` only. Hand the objects to a Flux tag instead.
  The stale-checkout guard that H1 ran beside it was retired in #5957 once H1.7b had removed its
  last row. A checkout older than H1.7b still carries its own guard, so this tree's empty copy
  protected nothing. To revive it, take it from #5316 in git history.
- **The host checks moved out with their contract.** The autonomous-role contract of the backup,
  disk and token checks is in `hosts/components/k3s-host-checks/component.yml`, and the checks are
  timers there rather than crons in this role.

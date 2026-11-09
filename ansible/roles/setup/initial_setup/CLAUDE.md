# initial_setup — emptied; the host packages own the host baseline

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/initial_setup/tasks/main.yml`. H7.63 emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions, and `docs/host-baseline-record.md` carries the
per-block traps and the journald-cap forensics.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "initial_setup"`; ends on hosts where `True`
  (leading `meta: end_role`)
- **Crons / timers:** none (the role ends on every host before its first task)
- **Packaged timers:** `homelab-aide-check.timer` (`OnCalendar=Mon *-*-* 03:00:00`) on
  daniel-box, daniel-pi, daniel-server; `homelab-apt-autoremove.timer` (`OnCalendar=Sun *-*-*
  02:00:00`) on daniel-box, daniel-pi, daniel-server; `homelab-docker-prune.timer`
  (`OnCalendar=*-*-* 06:30:00`) on daniel-pi; `homelab-dpkg-purge.timer` (`OnCalendar=Sun *-*-*
  02:15:00`) on daniel-box, daniel-pi, daniel-server; `homelab-firmware-update.timer`
  (`OnCalendar=Sun *-*-* 07:00:00`) on daniel-box; `homelab-reboot.timer` (`OnCalendar=Sun
  *-*-* 07:30:00 UTC`) on daniel-box, daniel-pi, daniel-server; `homelab-rkhunter-check.timer`
  (`OnCalendar=Wed *-*-* 02:00:00`) on daniel-box, daniel-pi, daniel-server
<!-- /generated_from -->

## Where the work went
The host packages own the whole baseline on every host: sshd, fail2ban, unattended upgrades,
sysctls, the journald cap and the rsyslog filter, the sudo cache, auditd, the kernel modules,
the banners, the base packages, the maintenance jobs, the integrity scans and the firewall.
Step 4 of `hosts/bootstrap-host.sh` covers the swap, the `ssh-users` group and the
networkd-dispatcher mask (H7.58 to H7.60). The cluster hosts' packages and the dotfiles repo
own the dev tooling, Vale and the worktree holder scan (H7.61, H7.62).

H7.63 deleted the rest, and each host package's `legacy:` list removes what it left:

- **The setup-drift check** compared rendered setup templates with the repo. Nothing renders
  setup templates once the deployer's timer is retired (H7.52), so the check, its library, its
  stamp fragments and its two alert rules went.
- **`/usr/local/lib/homelab-check-lib.sh`**, the Ansible-era copy of the host-check library.
  The `check-lib` and `pi-heartbeats` components ship `/usr/lib/homelab/homelab-check-lib.sh`.
- **The `Clear ansible log file` cron**, in root's crontab, is a legacy entry on all three
  hosts.
- **The `ansible-core` and `ansible-lint` uv tools and the collections install.** The tools and
  collections already installed stay until H7.56. The operator then runs `uv tool uninstall`
  for the two tools (**operator-only**). `ansible/README.md` has the collections command.

The role's `templates/` and `files/` stay as component inputs: `integrity-scans`, `fail2ban`
and `host-maintenance` render or copy them through `role: initial_setup`, `check-lib` and
`pi-heartbeats` ship `files/homelab-check-lib.sh`, and `worktree-holders` ships
`files/worktree_holders.py`. Grep `hosts/` before you delete one. A new host job goes in a
component.

## Rules the task files do not state
- **The journal cap and the rsyslog info filter are one change.** The cap ships in
  `hosts/components/host-tuning/templates/50-homelab-journald.conf`. The filter is the
  `rsyslog-filter` component's, and every host whose `has_rsyslog` is true lists it. The flag is
  false on the Pi. ENFORCED:
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_raising_the_forwarding_level_requires_the_syslog_filter`,
  `ansible/tests/setup/test_journald_syslog_forwarding.py::test_the_filter_component_lists_exactly_on_hosts_with_rsyslog`.
- **The role defines no handlers.** A `notify:` in a setup role resolves against every role in
  the play, so it can reach another role's handler. ENFORCED:
  `ansible/tests/setup/test_setup_handlers_resolve.py::test_every_notify_in_initial_setup_resolves_to_a_handler`,
  `ansible/tests/setup/test_setup_handlers_resolve.py::test_every_handler_in_the_play_has_a_notifier`.

## Rollback
A revert of H7.63 restores the task files, the drift check and their tests. The operator then
re-applies the role on each host after `homelab_host_applied_info` shows the reverted package
version: `cd /home/ubuntu/server && uv run ansible-playbook ansible/initial_setup.yml --tags
initial_setup` (operator-only), run on that host.

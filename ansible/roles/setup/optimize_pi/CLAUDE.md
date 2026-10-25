# optimize_pi — emptied; the Pi's host package owns the tuning

The role's tasks are one unconditional `meta: end_role`, under a `# DECIDED` marker in
`ansible/roles/setup/optimize_pi/tasks/main.yml`. H7.25 emptied it. It stays until H7.20a,
because `ansible/initial_setup.yml` names it and an edit to that playbook parks the deployer.
Repo-root `CLAUDE.md` has the conventions.

## At a glance
<!-- generated_from: scripts/docs/gen_role_glance.py -- do not edit between this line and the closing marker. Regenerate with `uv run python scripts/docs/gen_role_glance.py` after changing this role's tasks, timer templates, defaults or playbook entry, or a schedule var in group_vars/all.yml. -->
- **Applied by:** `initial_setup.yml --tags "optimize_pi"` when `inventory_hostname ==
  optimize_pi_host`; ends on hosts where `True` (leading `meta: end_role`)
- **Crons / timers:** none (no `ansible.builtin.cron` task in `tasks/`, no
  `templates/*.timer.j2`)
<!-- /generated_from -->

## Where the work went
`homelab-daniel-pi` ships every step. `docs/pi-host-tuning-record.md` has the measurements
behind each number, and each component's `component.yml` header carries the rules a session
must not break.

- `hosts/components/pi-tuning/` holds the GPU split, zram, Log2Ram's patched copy and its four
  declared values, the hardware watchdog, earlyoom's drop-in, the journald cap, sysstat, acct
  and auditd retention, the apt-timer drop-ins and the `/etc/profile.d/homelab.sh` export that
  `config_files` carried.
- `hosts/components/pi-node-exporter/` holds the node exporter unit, its `tmpfiles.d` line for
  `/var/lib/node-exporter-textfile`, and the `node_exporter` user. The binary is
  `homelab-tools`' `node-exporter`.
- `hosts/components/pi-resolver/` holds `/etc/resolv.conf` and the `127.0.1.1` hostname line.
- `hosts/components/pi-peer-backup-target/` holds pi-peer-backup's forced-command wrapper and
  its authorized key.
- The three heartbeats left in H3.8b (`hosts/components/pi-heartbeats/`).

## What stayed behind
- The debloat purge (Open vSwitch, snapd, fwupd), the rsyslog and `wpa_supplicant` masks,
  `systemd-resolved`'s disable and the one-off cleanups of stale rsyslog, AIDE and audit logs
  are state the live Pi already holds. The bootstrap debloat step carries them for a rebuild.
- The LAN's port-9100 ufw rule (`optimize_pi: node_exporter`) stays an Ansible-era rule until
  H7.30 declares the Pi's ufw rules.
- Installing `log2ram` stays a bootstrap step: Azlux's apt source arrives in the same package, so
  the package cannot depend on it.
- The Ansible-era edits stay in the files as harmless duplicates, and the interim push-token
  files under `/etc/homelab/interim/` are legacy entries of the Pi package.

## Rollback
A revert of H7.25 restores the tasks, and the operator then re-applies the role from a node over
ssh after `homelab_host_applied_info` shows the reverted version: `cd /home/ubuntu/server && uv
run ansible-playbook ansible/initial_setup.yml --tags optimize_pi -e target=daniel-pi`
(operator-only). The line arm never removes a line, so the edits stay in force through the
revert, and the textfile directory keeps its owner and mode.
